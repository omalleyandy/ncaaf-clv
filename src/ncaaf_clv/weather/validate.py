"""Data-quality validation for a weather snapshot: game coverage, join integrity, hourly detail.

Inputs: the weather payload (`hourly` + `game_window` rows), the ESPN games it was built from, and the
stadium registry. Same PASS / WARN / FAIL report shape as the odds validator, so reports load into
`validation_checks` unchanged.

    error  - rows that would corrupt a join or a feature (wrong team ids, indoor rows, out-of-window hours,
             impossible values, duplicates, unresolved outdoor venues)
    warn   - suspicious or incomplete (partial window coverage, gaps, state mismatch, stale forecast hours)
    info   - expected/explainable (games outside the API horizon, TBA kickoffs, in-game snapshots)
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from ncaaf_clv.common import parse_espn_datetime
from ncaaf_clv.config import SOURCE_ACCUWEATHER_FORECAST, SOURCE_ACCUWEATHER_OBSERVED
from ncaaf_clv.validate.validator import Check, build_report
from ncaaf_clv.weather.parse import (
    COVERED_STATUSES,
    HOUR,
    WINDOW_STATUS_INDOOR,
    WINDOW_STATUS_NO_KICKOFF,
    WINDOW_STATUS_OUTSIDE,
    WINDOW_STATUS_UNRESOLVED,
    expected_hours,
    game_window,
    hour_in_window,
    reachable_hours,
)
from ncaaf_clv.weather.stadiums import registry_index, same_stadium

log = logging.getLogger(__name__)

# Physical plausibility bounds for US/Ireland outdoor football venues (inclusive).
RANGES: dict[str, tuple[float, float]] = {
    "temp_f": (-40.0, 130.0),
    "real_feel_f": (-80.0, 150.0),
    "dew_point_f": (-60.0, 100.0),
    "relative_humidity": (0, 100),
    "wind_speed_mph": (0.0, 120.0),
    "wind_gust_mph": (0.0, 160.0),
    "wind_direction_deg": (0, 360),
    "precip_probability": (0, 100),
    "rain_probability": (0, 100),
    "snow_probability": (0, 100),
    "ice_probability": (0, 100),
    "thunderstorm_probability": (0, 100),
    "cloud_cover": (0, 100),
    "visibility_mi": (0.0, 200.0),
    "uv_index": (0, 16),
    "total_liquid_in": (0.0, 6.0),
    "pressure_inhg": (25.0, 32.5),
}
REQUIRED_FORECAST_FIELDS = ("temp_f", "wind_speed_mph", "precip_probability", "relative_humidity")
REQUIRED_OBSERVED_FIELDS = ("temp_f", "wind_speed_mph", "relative_humidity")
STALE_FORECAST_TOL_H = 1.0  # forecast hours may start up to 1 h before the snapshot (the current hour)


def _game_index(games: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {g["game_id"]: g for g in games}


def _recompute_reachable(w: dict[str, Any], games: dict[str, dict[str, Any]]) -> int:
    """Independent recomputation of the window row's hours_reachable from the ESPN kickoff and the snapshot."""
    g = games.get(w["game_id"]) or {}
    kickoff = parse_espn_datetime(g.get("kickoff_utc") or w.get("kickoff_utc"))
    snapshot = parse_espn_datetime(w.get("snapshot_utc"))
    if kickoff is None or snapshot is None:
        return 0
    start = parse_espn_datetime(w.get("window_start_utc"))
    end = parse_espn_datetime(w.get("window_end_utc"))
    if start is None or end is None:
        return 0
    return len(reachable_hours(expected_hours((start, end)), snapshot, w.get("source")))


def check_weather_join(
    hourly: list[dict[str, Any]], windows: list[dict[str, Any]], games: dict[str, dict[str, Any]]
) -> list[Check]:
    checks: list[Check] = []
    rows = [("hourly", r) for r in hourly] + [("game_window", r) for r in windows]

    c = Check("wx_game_ids_exist", "error", "every weather row's game_id is an ESPN game in the snapshot")
    for kind, r in rows:
        c.observed += 1
        if r.get("game_id") not in games:
            c.add(table=kind, game_id=r.get("game_id"), snapshot_utc=r.get("snapshot_utc"))
    checks.append(c)

    c = Check("wx_team_ids_match_game", "error", "home_team_id / away_team_id equal the ESPN game's team ids")
    for kind, r in rows:
        g = games.get(r.get("game_id"))
        if not g:
            continue
        c.observed += 1
        if r.get("home_team_id") != g.get("home_team_id") or r.get("away_team_id") != g.get("away_team_id"):
            c.add(
                table=kind,
                game_id=r["game_id"],
                row_home=r.get("home_team_id"),
                row_away=r.get("away_team_id"),
                espn_home=g.get("home_team_id"),
                espn_away=g.get("away_team_id"),
            )
    checks.append(c)

    c = Check("wx_venue_matches_game", "error", "row venue_id equals the ESPN game's venue_id")
    for kind, r in rows:
        g = games.get(r.get("game_id"))
        if not g:
            continue
        c.observed += 1
        if str(r.get("venue_id")) != str(g.get("venue_id")):
            c.add(table=kind, game_id=r["game_id"], row_venue=r.get("venue_id"), espn_venue=g.get("venue_id"))
    checks.append(c)

    c = Check("wx_kickoff_matches_game", "error", "row kickoff_utc equals the ESPN game's kickoff_utc")
    for kind, r in rows:
        g = games.get(r.get("game_id"))
        if not g:
            continue
        c.observed += 1
        if r.get("kickoff_utc") != g.get("kickoff_utc"):
            c.add(table=kind, game_id=r["game_id"], row_kickoff=r.get("kickoff_utc"), espn_kickoff=g.get("kickoff_utc"))
    checks.append(c)

    c = Check(
        "wx_hourly_rows_have_window",
        "error",
        "every game with hourly rows has a game_window row (same source/snapshot)",
    )
    win_keys = {(w["game_id"], w.get("source"), w["snapshot_utc"]) for w in windows}
    for r in hourly:
        c.observed += 1
        if (r["game_id"], r.get("source"), r["snapshot_utc"]) not in win_keys:
            c.add(game_id=r["game_id"], source=r.get("source"), snapshot_utc=r["snapshot_utc"])
            break
    checks.append(c)
    return checks


def check_weather_coverage(
    windows: list[dict[str, Any]],
    hourly: list[dict[str, Any]],
    games: dict[str, dict[str, Any]],
    registry: dict[str, dict[str, Any]],
) -> list[Check]:
    checks: list[Check] = []
    hours_by_game: dict[tuple[str, str | None, str], list[dict[str, Any]]] = {}
    for r in hourly:
        hours_by_game.setdefault((r["game_id"], r.get("source"), r["snapshot_utc"]), []).append(r)

    c = Check(
        "wx_outdoor_games_have_location",
        "error",
        "every outdoor game the run tried to cover resolves to a location key (status unresolved_location = gap)",
    )
    for w in windows:
        if w.get("is_indoor") or w.get("status") in (WINDOW_STATUS_OUTSIDE, WINDOW_STATUS_NO_KICKOFF):
            continue  # not attempted: nothing to resolve yet
        c.observed += 1
        if w.get("status") == WINDOW_STATUS_UNRESOLVED or not w.get("location_key"):
            c.add(game_id=w["game_id"], venue_id=w.get("venue_id"), venue=w.get("venue_name"), status=w.get("status"))
    checks.append(c)

    c = Check(
        "wx_indoor_games_have_no_hours", "error", "indoor / dome games carry no hourly rows and status indoor_skipped"
    )
    for w in windows:
        if not w.get("is_indoor"):
            continue
        c.observed += 1
        n = len(hours_by_game.get((w["game_id"], w.get("source"), w["snapshot_utc"]), []))
        if n or w.get("status") != WINDOW_STATUS_INDOOR:
            c.add(game_id=w["game_id"], venue=w.get("venue_name"), hourly_rows=n, status=w.get("status"))
    checks.append(c)

    c = Check(
        "wx_indoor_flag_matches_registry",
        "error",
        "is_indoor on the window row agrees with the stadium registry / ESPN flag",
    )
    for w in windows:
        reg = registry.get(str(w.get("venue_id")))
        g = games.get(w["game_id"], {})
        if reg is None and g.get("venue_indoor") is None:
            continue
        c.observed += 1
        expected = bool(reg["is_indoor"]) if reg is not None else bool(g.get("venue_indoor"))
        if bool(w.get("is_indoor")) != expected:
            c.add(game_id=w["game_id"], venue=w.get("venue_name"), row_indoor=w.get("is_indoor"), expected=expected)
    checks.append(c)

    c = Check(
        "wx_window_coverage_complete",
        "warn",
        "covered games have an hourly row for every reachable hour of the kickoff window "
        "(short = horizon too short at snapshot time: run again closer to kickoff)",
    )
    for w in windows:
        if w.get("status") not in COVERED_STATUSES:
            continue
        c.observed += 1
        reachable = _recompute_reachable(w, games) if w.get("hours_reachable") is not None else 0
        if (w.get("hours_present") or 0) < reachable:
            c.add(
                game_id=w["game_id"],
                venue=w.get("venue_name"),
                hours_expected=w.get("hours_expected"),
                hours_reachable=reachable,
                hours_present=w.get("hours_present"),
                first_hour=w.get("first_hour_utc"),
                last_hour=w.get("last_hour_utc"),
                lead_hours_at_kickoff=w.get("lead_hours_at_kickoff"),
            )
    checks.append(c)

    c = Check(
        "wx_window_reachable_consistent",
        "error",
        "hours_reachable on the window row matches the API rule (next full hour)",
    )
    for w in windows:
        if w.get("status") not in COVERED_STATUSES or w.get("hours_reachable") is None:
            continue
        c.observed += 1
        expected = _recompute_reachable(w, games)
        if expected != w.get("hours_reachable"):
            c.add(game_id=w["game_id"], hours_reachable=w.get("hours_reachable"), expected=expected)
    checks.append(c)

    c = Check(
        "wx_window_partially_elapsed",
        "info",
        "window hours already elapsed at snapshot time (in-game forecast snapshot)",
    )
    for w in windows:
        if w.get("status") not in COVERED_STATUSES or w.get("source") == SOURCE_ACCUWEATHER_OBSERVED:
            continue
        c.observed += 1
        if (w.get("hours_reachable") or 0) < (w.get("hours_expected") or 0):
            c.add(
                game_id=w["game_id"],
                hours_expected=w.get("hours_expected"),
                hours_reachable=w.get("hours_reachable"),
                lead_hours_at_kickoff=w.get("lead_hours_at_kickoff"),
            )
    checks.append(c)

    c = Check(
        "wx_window_counts_match_rows",
        "error",
        "hours_present on the window row equals the number of in-window hourly rows",
    )
    for w in windows:
        if w.get("status") not in COVERED_STATUSES:
            continue
        c.observed += 1
        rows = hours_by_game.get((w["game_id"], w.get("source"), w["snapshot_utc"]), [])
        n = len({r["forecast_for_utc"] for r in rows if r.get("in_game_window")})
        if n != (w.get("hours_present") or 0):
            c.add(game_id=w["game_id"], hours_present=w.get("hours_present"), in_window_rows=n)
    checks.append(c)

    c = Check("wx_hours_contiguous", "warn", "a game's hourly rows are consecutive hours (no gaps)")
    for key, rows in hours_by_game.items():
        times = sorted({parse_espn_datetime(r["forecast_for_utc"]) for r in rows if r.get("forecast_for_utc")})
        if len(times) < 2:
            continue
        c.observed += 1
        gaps = [(a, b) for a, b in zip(times, times[1:], strict=False) if b - a != HOUR]
        if gaps:
            c.add(game_id=key[0], source=key[1], gaps=[f"{a:%H:%M}->{b:%H:%M}Z" for a, b in gaps[:5]])
    checks.append(c)

    c = Check("wx_games_outside_horizon", "info", "in-scope games whose window the API horizon could not reach yet")
    for w in windows:
        c.observed += 1
        if w.get("status") == WINDOW_STATUS_OUTSIDE:
            c.add(
                game_id=w["game_id"],
                kickoff_utc=w.get("kickoff_utc"),
                lead_hours_at_kickoff=w.get("lead_hours_at_kickoff"),
            )
    checks.append(c)

    c = Check(
        "wx_kickoff_time_valid",
        "warn",
        "covered games have a firm kickoff (ESPN time_valid); TBA slots cannot be windowed",
    )
    for w in windows:
        g = games.get(w["game_id"])
        if not g or w.get("status") not in COVERED_STATUSES:
            continue
        c.observed += 1
        if g.get("time_valid") is False or not g.get("kickoff_utc"):
            c.add(game_id=w["game_id"], time_valid=g.get("time_valid"), kickoff_utc=g.get("kickoff_utc"))
    checks.append(c)

    c = Check("wx_location_state_matches_venue", "warn", "resolved US location's state equals the ESPN venue state")
    for w in windows:
        reg = registry.get(str(w.get("venue_id")))
        if not reg or not reg.get("location_key") or reg.get("country_code") not in (None, "US"):
            continue
        if not reg.get("venue_state") or not reg.get("admin_area"):
            continue
        c.observed += 1
        if reg["admin_area"] != reg["venue_state"]:
            c.add(
                venue_id=reg["venue_id"],
                venue=reg.get("venue_name"),
                venue_state=reg["venue_state"],
                location_state=reg["admin_area"],
                key=reg["location_key"],
            )
    checks.append(c)

    c = Check(
        "wx_location_key_unique_per_venue",
        "warn",
        "no two outdoor venues share an AccuWeather location key (city-level fallback in use)",
    )
    by_key: dict[str, list[str]] = {}
    for reg in registry.values():
        if reg.get("location_key") and not reg.get("is_indoor"):
            by_key.setdefault(reg["location_key"], []).append(f"{reg['venue_id']} {reg.get('venue_name')}")
    for key, venues in by_key.items():
        c.observed += 1
        if len(venues) > 1 and not same_stadium(v.split(" ", 1)[0] for v in venues):
            c.add(location_key=key, venues=venues)
    checks.append(c)
    return checks


def check_weather_detail(
    hourly: list[dict[str, Any]], games: dict[str, dict[str, Any]], pre_hours: float, duration_hours: float
) -> list[Check]:
    checks: list[Check] = []

    c = Check("wx_no_duplicate_hours", "error", "unique (game_id, source, forecast_for_utc, snapshot_utc)")
    seen: set[tuple] = set()
    for r in hourly:
        c.observed += 1
        key = (r["game_id"], r.get("source"), r.get("forecast_for_utc"), r["snapshot_utc"])
        if key in seen:
            c.add(game_id=r["game_id"], source=r.get("source"), forecast_for_utc=r.get("forecast_for_utc"))
        seen.add(key)
    checks.append(c)

    c = Check(
        "wx_hours_within_window",
        "error",
        f"in_game_window rows fall in [kickoff-{pre_hours}h, kickoff+{duration_hours}h]; offsets consistent",
    )
    for r in hourly:
        g = games.get(r["game_id"])
        kickoff = parse_espn_datetime((g or {}).get("kickoff_utc"))
        hour = parse_espn_datetime(r.get("forecast_for_utc"))
        if not kickoff or not hour:
            c.observed += 1
            c.add(
                game_id=r["game_id"],
                forecast_for_utc=r.get("forecast_for_utc"),
                kickoff_utc=(g or {}).get("kickoff_utc"),
            )
            continue
        c.observed += 1
        window = game_window(kickoff, pre_hours, duration_hours)
        inside = hour_in_window(hour, window)
        offset = (hour - kickoff).total_seconds() / 3600.0
        if bool(r.get("in_game_window")) != inside or abs((r.get("hour_offset_from_kickoff") or 0) - offset) > 0.01:
            c.add(
                game_id=r["game_id"],
                forecast_for_utc=r["forecast_for_utc"],
                kickoff_utc=g["kickoff_utc"],
                in_game_window=r.get("in_game_window"),
                expected_in_window=inside,
                hour_offset=r.get("hour_offset_from_kickoff"),
            )
    checks.append(c)

    c = Check(
        "wx_values_in_range",
        "error",
        "temperatures, humidity, wind, probabilities, cloud cover, UV, visibility within physical bounds",
    )
    for r in hourly:
        for f, (lo, hi) in RANGES.items():
            v = r.get(f)
            if v is None:
                continue
            c.observed += 1
            if not (lo <= v <= hi):
                c.add(
                    game_id=r["game_id"], forecast_for_utc=r.get("forecast_for_utc"), field=f, value=v, range=[lo, hi]
                )
    checks.append(c)

    c = Check(
        "wx_required_fields_present",
        "warn",
        "temperature, wind, humidity (and precip probability for forecasts) are populated",
    )
    for r in hourly:
        req = REQUIRED_OBSERVED_FIELDS if r.get("source") == SOURCE_ACCUWEATHER_OBSERVED else REQUIRED_FORECAST_FIELDS
        c.observed += 1
        missing = [f for f in req if r.get(f) is None]
        if missing:
            c.add(game_id=r["game_id"], forecast_for_utc=r.get("forecast_for_utc"), missing=missing)
    checks.append(c)

    c = Check("wx_dew_point_le_temp", "warn", "dew point does not exceed the air temperature (+1F tolerance)")
    for r in hourly:
        t, d = r.get("temp_f"), r.get("dew_point_f")
        if t is None or d is None:
            continue
        c.observed += 1
        if d > t + 1.0:
            c.add(game_id=r["game_id"], forecast_for_utc=r.get("forecast_for_utc"), temp_f=t, dew_point_f=d)
    checks.append(c)

    c = Check("wx_gust_ge_wind", "warn", "wind gust is at least the sustained wind speed")
    for r in hourly:
        w, gst = r.get("wind_speed_mph"), r.get("wind_gust_mph")
        if w is None or gst is None:
            continue
        c.observed += 1
        if gst + 0.01 < w:
            c.add(game_id=r["game_id"], forecast_for_utc=r.get("forecast_for_utc"), wind=w, gust=gst)
    checks.append(c)

    c = Check(
        "wx_precip_flags_consistent",
        "warn",
        "has_precipitation implies a precipitation type and a non-zero probability/amount",
    )
    for r in hourly:
        if not r.get("has_precipitation"):
            continue
        c.observed += 1
        amount = r.get("total_liquid_in") or r.get("precip_1hr_in") or 0
        prob = r.get("precip_probability")
        if not r.get("precipitation_type") or (prob == 0 and amount == 0):
            c.add(
                game_id=r["game_id"],
                forecast_for_utc=r.get("forecast_for_utc"),
                type=r.get("precipitation_type"),
                probability=prob,
                amount=amount,
            )
    checks.append(c)

    c = Check(
        "wx_forecast_lead_consistent",
        "warn",
        "forecast hours are not in the past at snapshot time; observed hours are not in the future",
    )
    for r in hourly:
        lead = r.get("lead_hours")
        if lead is None:
            continue
        c.observed += 1
        if r.get("source") == SOURCE_ACCUWEATHER_OBSERVED:
            if lead > 0:
                c.add(game_id=r["game_id"], source=r["source"], forecast_for_utc=r["forecast_for_utc"], lead_hours=lead)
        elif lead < -STALE_FORECAST_TOL_H:
            c.add(game_id=r["game_id"], source=r["source"], forecast_for_utc=r["forecast_for_utc"], lead_hours=lead)
    checks.append(c)

    c = Check(
        "wx_forecast_snapshot_pre_kickoff",
        "info",
        "forecast snapshots taken after kickoff are in-game forecasts, not pre-game",
    )
    seen_games: set[tuple[str, str]] = set()
    for r in hourly:
        if r.get("source") != SOURCE_ACCUWEATHER_FORECAST:
            continue
        key = (r["game_id"], r["snapshot_utc"])
        if key in seen_games:
            continue
        seen_games.add(key)
        kickoff = parse_espn_datetime(r.get("kickoff_utc"))
        snap = parse_espn_datetime(r["snapshot_utc"])
        if not kickoff or not snap:
            continue
        c.observed += 1
        if snap > kickoff:
            c.add(
                game_id=r["game_id"],
                snapshot_utc=r["snapshot_utc"],
                kickoff_utc=r["kickoff_utc"],
                minutes_after=round((snap - kickoff) / timedelta(minutes=1)),
            )
    checks.append(c)
    return checks


def validate_weather(
    weather: dict[str, Any],
    games: list[dict[str, Any]],
    registry: dict[str, Any] | None,
    pre_hours: float | None = None,
    duration_hours: float | None = None,
) -> dict[str, Any]:
    """Run every weather check against `weather` (`hourly`, `game_window`, `meta`) and return the report."""
    meta = weather.get("meta") or {}
    pre_hours = meta.get("pre_kickoff_hours", pre_hours) if pre_hours is None else pre_hours
    duration_hours = meta.get("game_duration_hours", duration_hours) if duration_hours is None else duration_hours
    if pre_hours is None or duration_hours is None:
        raise ValueError("window bounds missing: pass pre_hours/duration_hours or a payload with meta bounds")
    hourly = weather.get("hourly") or []
    windows = weather.get("game_window") or []
    gidx = _game_index(games)
    reg = registry_index(registry) if registry else {}
    checks = check_weather_join(hourly, windows, gidx)
    checks += check_weather_coverage(windows, hourly, gidx, reg)
    checks += check_weather_detail(hourly, gidx, float(pre_hours), float(duration_hours))
    report = build_report(checks, meta.get("snapshot_utc"))
    report["meta"].update(
        {
            "kind": "weather",
            "hourly_rows": len(hourly),
            "game_window_rows": len(windows),
            "status_counts": {
                s: sum(1 for w in windows if w.get("status") == s) for s in sorted({w.get("status") for w in windows})
            },
        }
    )
    return report
