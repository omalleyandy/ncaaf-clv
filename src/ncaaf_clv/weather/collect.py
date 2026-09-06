"""Weather collection orchestration.

    run_weather_locations   ESPN venues (from processed snapshots or the DB) -> stadium registry with AccuWeather keys
    run_weather_pipeline    games in an ESPN snapshot -> one AccuWeather call per outdoor location ->
                            data/processed/weather/<snapshot>/hourly.json  (meta, hourly, game_window, locations)

Modes:
    forecast   pre-game: `forecasts/v1/hourly/{N}hour` for every outdoor game whose kickoff window overlaps
               [now, now + N h]. Each run is one snapshot, so repeated runs (T-24h, T-12h, T-3h ...) build a
               forecast history per game exactly like the odds snapshots do.
    observed   post-game: `currentconditions/v1/{key}/historical/24` for games whose window started in the
               last 24 h -> the conditions that actually prevailed during the game (same columns, source
               accuweather_observed). Run it the morning after a slate.

Scope rules: indoor / dome games are recorded with status `indoor_skipped` (no API call, no hourly rows);
venues without a location key are `unresolved_location`; TBA kickoffs (`time_valid` false) are `no_kickoff`;
games whose window the horizon cannot reach are `outside_horizon`. Everything in scope gets a game_window row.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ncaaf_clv.common import iso_utc, parse_espn_datetime, read_json, snapshot_tag, utc_now, write_json
from ncaaf_clv.config import SOURCE_ACCUWEATHER_FORECAST, SOURCE_ACCUWEATHER_OBSERVED, Settings
from ncaaf_clv.espn.client import ESPNClient
from ncaaf_clv.weather.client import AccuWeatherAuthError, AccuWeatherClient, AccuWeatherError
from ncaaf_clv.weather.parse import (
    WINDOW_STATUS_API_ERROR,
    WINDOW_STATUS_FORECAST,
    WINDOW_STATUS_INDOOR,
    WINDOW_STATUS_NO_KICKOFF,
    WINDOW_STATUS_OBSERVED,
    WINDOW_STATUS_OUTSIDE,
    WINDOW_STATUS_UNRESOLVED,
    game_window,
    parse_game_hours,
    summarize_game_window,
)
from ncaaf_clv.weather.stadiums import (
    load_registry,
    merge_venues,
    registry_index,
    resolve_registry,
    save_registry,
    venues_from_db,
    venues_from_games,
    venues_from_snapshots,
)

log = logging.getLogger(__name__)

MODE_FORECAST = "forecast"
MODE_OBSERVED = "observed"
OBSERVED_LOOKBACK_H = 24.0
FORECAST_STALE_TOL_H = 1.0  # the API's first hour is the current (partially elapsed) hour


# ------------------------------------------------------------------------------ snapshots


def espn_snapshot_dirs(settings: Settings) -> list[Path]:
    root = settings.processed_dir / "espn"
    return sorted(p for p in root.glob("*/") if (p / "scoreboard.json").exists()) if root.exists() else []


def latest_espn_snapshot(settings: Settings) -> str | None:
    """Most recent full weekly ESPN snapshot tag (not backfill-<year>, not refresh-<tag>: those hold only
    a season's or the still-open games); tags are UTC timestamps so lexical order is time order."""
    live = [p.name for p in espn_snapshot_dirs(settings) if not p.name.startswith(("backfill-", "refresh-"))]
    return max(live) if live else None


def load_espn_games(settings: Settings, espn_snapshot: str) -> list[dict[str, Any]]:
    return read_json(settings.processed_dir / "espn" / espn_snapshot / "scoreboard.json")["games"]


# ------------------------------------------------------------------------------ locations


async def run_weather_locations(
    settings: Settings,
    espn_snapshots: Iterable[str] | None = None,
    db_path: Path | None = None,
    refresh: bool = False,
    include_indoor: bool = False,
    limit: int | None = None,
    resolve: bool = True,
    venue_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Build / extend the stadium registry from ESPN venues and resolve AccuWeather keys for outdoor venues."""
    if db_path is not None:
        venues = venues_from_db(db_path)
        source = str(db_path)
    else:
        dirs = (
            [settings.processed_dir / "espn" / s for s in espn_snapshots]
            if espn_snapshots
            else espn_snapshot_dirs(settings)
        )
        venues = venues_from_snapshots(dirs)
        source = ", ".join(d.name for d in dirs)
    registry = merge_venues(load_registry(settings.weather_locations_file), venues)
    log.info("registry: %d venues from %s", len(registry["venues"]), source)
    stats: dict[str, Any] = {"venues": len(registry["venues"]), "resolution": None}
    if resolve:
        if not settings.accuweather_api_key:
            log.warning("ACCUWEATHER_API_KEY not set: registry written without location keys")
            stats["resolution"] = "skipped: no api key"
        else:
            async with (
                AccuWeatherClient(settings, raw_dir=settings.raw_weather_dir) as aw,
                ESPNClient(settings) as espn,
            ):
                try:
                    stats["resolution"] = await resolve_registry(
                        aw,
                        espn,
                        registry,
                        only_unresolved=not refresh,
                        include_indoor=include_indoor,
                        limit=limit,
                        venue_ids=venue_ids,
                    )
                finally:
                    stats["api_calls"] = aw.calls_made
    out = save_registry(settings.weather_locations_file, registry)
    stats.update({"file": str(out), **registry["meta"]})
    return stats


# ------------------------------------------------------------------------------ scope


def _kickoff_local_date(kickoff: datetime, tz: str) -> str:
    return kickoff.astimezone(ZoneInfo(tz)).strftime("%Y%m%d")


def games_in_scope(
    games: list[dict[str, Any]],
    mode: str,
    now: datetime,
    horizon_hours: float,
    pre_hours: float,
    duration_hours: float,
    game_ids: Iterable[str] | None = None,
    dates: Iterable[str] | None = None,
    local_tz: str = "America/New_York",
) -> list[tuple[dict[str, Any], str | None]]:
    """(game, preset_status) for games the run should record. `None` status = fetch weather.

    forecast: window overlaps [now - 1h, now + horizon]; a game requested explicitly (game_ids/dates) whose window
              lies beyond the horizon is recorded as outside_horizon instead of being dropped.
    observed: window started within the last 24 h and has begun (historical/24 covers exactly that range).
    """
    wanted_ids = set(game_ids or [])
    wanted_dates = set(dates or [])
    explicit = bool(wanted_ids or wanted_dates)
    out: list[tuple[dict[str, Any], str | None]] = []
    for g in games:
        if wanted_ids and g["game_id"] not in wanted_ids:
            continue
        kickoff = parse_espn_datetime(g.get("kickoff_utc"))
        if kickoff is None:
            if explicit:
                out.append((g, WINDOW_STATUS_NO_KICKOFF))
            continue
        if wanted_dates and _kickoff_local_date(kickoff, local_tz) not in wanted_dates:
            continue
        if g.get("time_valid") is False:
            if explicit or mode == MODE_FORECAST:
                out.append((g, WINDOW_STATUS_NO_KICKOFF))
            continue
        start, end = game_window(kickoff, pre_hours, duration_hours)
        if mode == MODE_FORECAST:
            reach = now + timedelta(hours=horizon_hours)
            if end <= now - timedelta(hours=FORECAST_STALE_TOL_H):
                if explicit:
                    out.append((g, WINDOW_STATUS_OUTSIDE))
                continue  # already played
            if start >= reach:
                if explicit or start - reach <= timedelta(hours=24):
                    out.append((g, WINDOW_STATUS_OUTSIDE))  # visible on the slate, not reachable yet
                continue
            out.append((g, None))
        elif mode == MODE_OBSERVED:
            lookback = now - timedelta(hours=OBSERVED_LOOKBACK_H)
            if start > now:
                continue  # not played yet
            if end < lookback:
                if explicit:
                    out.append((g, WINDOW_STATUS_OUTSIDE))
                continue  # older than the 24 h history the API keeps
            out.append((g, None))
        else:
            raise ValueError(f"unknown mode {mode!r}")
    return out


# ------------------------------------------------------------------------------ pipeline


async def run_weather_pipeline(
    settings: Settings,
    mode: str = MODE_FORECAST,
    espn_snapshot: str | None = None,
    snapshot: str | None = None,
    game_ids: Iterable[str] | None = None,
    dates: Iterable[str] | None = None,
    keep_all_hours: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Collect hourly weather for the games in an ESPN snapshot. Returns a manifest with the output path."""
    now = now or utc_now()
    snapshot = snapshot or snapshot_tag(now)
    snapshot_utc = iso_utc(now)
    espn_snapshot = espn_snapshot or latest_espn_snapshot(settings)
    if not espn_snapshot:
        raise FileNotFoundError("no ESPN snapshot found under data/processed/espn; run `ncaaf-clv espn` first")
    games = load_espn_games(settings, espn_snapshot)
    source = SOURCE_ACCUWEATHER_FORECAST if mode == MODE_FORECAST else SOURCE_ACCUWEATHER_OBSERVED
    pre, dur = settings.weather_pre_kickoff_hours, settings.weather_game_duration_hours
    horizon = settings.accuweather_hourly_hours if mode == MODE_FORECAST else OBSERVED_LOOKBACK_H
    scope = games_in_scope(games, mode, now, horizon, pre, dur, game_ids, dates, settings.overtime_tz)
    log.info("%s: %d games in scope from ESPN snapshot %s (%d games)", mode, len(scope), espn_snapshot, len(games))

    # Registry: make sure every venue in scope is known; resolve new outdoor venues on the fly.
    registry = merge_venues(load_registry(settings.weather_locations_file), venues_from_games(g for g, _ in scope))
    reg = registry_index(registry)
    needs_key = [
        reg[str(g["venue_id"])]
        for g, st in scope
        if st is None
        and g.get("venue_id")
        and not reg[str(g["venue_id"])]["is_indoor"]
        and not reg[str(g["venue_id"])]["location_key"]
    ]

    hourly: list[dict[str, Any]] = []
    windows: list[dict[str, Any]] = []
    api_calls = 0
    stopped: str | None = None
    payload_by_key: dict[str, list[dict[str, Any]] | Exception] = {}

    if not settings.accuweather_api_key:
        stopped = "ACCUWEATHER_API_KEY not set"
        log.error(stopped)
    else:
        async with AccuWeatherClient(settings, raw_dir=settings.raw_weather_dir / snapshot) as aw:
            try:
                if needs_key:
                    async with ESPNClient(settings) as espn:
                        uniq = {v["venue_id"]: v for v in needs_key}
                        sub = {"venues": list(uniq.values()), "meta": {}}
                        await resolve_registry(aw, espn, sub, only_unresolved=True)
                # One call per distinct location key.
                keys = sorted(
                    {
                        reg[str(g["venue_id"])]["location_key"]
                        for g, st in scope
                        if st is None
                        and g.get("venue_id")
                        and reg[str(g["venue_id"])].get("location_key")
                        and not reg[str(g["venue_id"])]["is_indoor"]
                    }
                )
                for key in keys:
                    try:
                        if mode == MODE_FORECAST:
                            payload_by_key[key] = await aw.hourly_forecast(key, cache_key=f"hourly_{key}")
                        else:
                            payload_by_key[key] = await aw.historical_24h(key, cache_key=f"historical24_{key}")
                    except AccuWeatherError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - one bad location must not sink the slate
                        log.exception("location %s failed", key)
                        payload_by_key[key] = exc
            except AccuWeatherError as exc:
                stopped = str(exc)
                log.error("stopping weather run: %s", exc)
            finally:
                api_calls = aw.calls_made
        save_registry(settings.weather_locations_file, registry)  # persists any on-the-fly resolutions

    for g, preset in scope:
        venue = reg.get(str(g.get("venue_id"))) if g.get("venue_id") else None
        is_indoor = bool(venue and venue.get("is_indoor"))
        roof = venue.get("roof_type") if venue else None
        status, note, rows = preset, None, []
        if status is None and is_indoor:
            status = WINDOW_STATUS_INDOOR
        elif status is None and not (venue and venue.get("location_key")):
            status, note = (
                WINDOW_STATUS_UNRESOLVED,
                "; ".join((venue or {}).get("notes") or []) or "venue not in registry",
            )
        elif status is None:
            payload = payload_by_key.get(venue["location_key"])
            if payload is None:
                status, note = WINDOW_STATUS_API_ERROR, stopped or "no payload"
            elif isinstance(payload, Exception):
                status, note = WINDOW_STATUS_API_ERROR, str(payload)
            else:
                rows = parse_game_hours(payload, g, venue, snapshot_utc, pre, dur, source, keep_all_hours)
                status = WINDOW_STATUS_FORECAST if mode == MODE_FORECAST else WINDOW_STATUS_OBSERVED
                if not rows:
                    note = f"{len(payload)} hours returned, none in the game window"
        hourly.extend(rows)
        windows.append(
            summarize_game_window(g, venue, rows, snapshot_utc, pre, dur, status, source, is_indoor, roof, note)
        )

    status_counts = {s: sum(1 for w in windows if w["status"] == s) for s in sorted({w["status"] for w in windows})}
    meta = {
        "source": source,
        "mode": mode,
        "snapshot": snapshot,
        "snapshot_utc": snapshot_utc,
        "espn_snapshot": espn_snapshot,
        "horizon_hours": horizon,
        "pre_kickoff_hours": pre,
        "game_duration_hours": dur,
        "keep_all_hours": keep_all_hours,
        "games_in_snapshot": len(games),
        "games_in_scope": len(scope),
        "locations_called": len(payload_by_key),
        "api_calls": api_calls,
        "hourly_rows": len(hourly),
        "status_counts": status_counts,
        "stopped": stopped,
    }
    used = sorted({str(g["venue_id"]) for g, _ in scope if g.get("venue_id")})
    out = {
        "meta": meta,
        "hourly": hourly,
        "game_window": windows,
        "locations": [reg[v] for v in used if v in reg],
    }
    path = write_json(settings.processed_dir / "weather" / snapshot / "hourly.json", out)
    log.info("%s: %d hourly rows for %d games -> %s (%s)", mode, len(hourly), len(windows), path, status_counts)
    return {**meta, "out": str(path)}


__all__ = [
    "MODE_FORECAST",
    "MODE_OBSERVED",
    "AccuWeatherAuthError",
    "games_in_scope",
    "latest_espn_snapshot",
    "load_espn_games",
    "run_weather_locations",
    "run_weather_pipeline",
]
