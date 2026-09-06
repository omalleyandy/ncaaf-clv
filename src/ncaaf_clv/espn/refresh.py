"""Refresh open games: re-collect the games that were not yet final at their last snapshot.

`espn_games` is point-in-time (keyed on game_id + snapshot_utc), so a refresh never updates rows in
place. It writes a new `refresh-<tag>` snapshot that holds only the games whose latest row in SQLite
is still open (status_state != 'post'), re-fetches the week scoreboard(s) those games belong to, and
collects the full summary (box score, drives/plays, core-API sportsbook lines incl. the close) for the
ones that have kicked off. Loading the snapshot appends rows; `v_espn_games_latest` exposes the
current state of every game, so a game that was live at 20:00Z and final at 23:00Z keeps both rows.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from ncaaf_clv.common import snapshot_tag, utc_now, write_json
from ncaaf_clv.config import Settings
from ncaaf_clv.espn.client import ESPNClient
from ncaaf_clv.espn.collect import ESPNCollector, _meta

log = logging.getLogger(__name__)

REFRESH_PREFIX = "refresh-"
TERMINAL_STATE = "post"  # ESPN status.type.state: pre | in | post (final, cancelled and postponed)
COLLECT_STATES = ("in", "post")  # kicked off or finished: worth a summary + core-odds fetch

OPEN_GAMES_SQL = """
WITH latest AS (
    SELECT game_id, MAX(snapshot_utc) AS snapshot_utc
    FROM espn_games
    WHERE season_year = ?
    GROUP BY game_id
)
SELECT g.game_id, g.short_name, g.season_year, g.season_type, g.week, g.kickoff_utc,
       g.status_name, g.status_state, g.snapshot_utc
FROM espn_games g
JOIN latest l ON l.game_id = g.game_id AND l.snapshot_utc = g.snapshot_utc
WHERE COALESCE(g.status_state, '') != ?
ORDER BY g.kickoff_utc, g.game_id
"""


def open_games(conn: sqlite3.Connection, season_year: int) -> list[dict[str, Any]]:
    """Games whose latest `espn_games` row is not terminal (scheduled, in progress, delayed, ...).

    Cancelled and postponed games report `status_state='post'` and are not retried."""
    cur = conn.execute(OPEN_GAMES_SQL, (season_year, TERMINAL_STATE))
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


def refresh_snapshot_tag(now: datetime | None = None) -> str:
    return f"{REFRESH_PREFIX}{snapshot_tag(now or utc_now())}"


def plan_refresh(scoreboard_games: list[dict[str, Any]], open_ids: Iterable[str]) -> dict[str, list[str]]:
    """Split the open set by the fresh scoreboard: `collect` (kicked off or finished: fetch the summary),
    `scoreboard_only` (still pre-game: the scoreboard row is the update) and `missing` (ESPN no longer
    lists the game under that week, e.g. moved; it stays open and is retried next time)."""
    by_id = {g["game_id"]: g for g in scoreboard_games}
    plan: dict[str, list[str]] = {"collect": [], "scoreboard_only": [], "missing": []}
    for gid in open_ids:
        game = by_id.get(gid)
        if game is None:
            plan["missing"].append(gid)
        elif game.get("status_state") in COLLECT_STATES:
            plan["collect"].append(gid)
        else:
            plan["scoreboard_only"].append(gid)
    return plan


async def run_refresh_pipeline(
    settings: Settings,
    conn: sqlite3.Connection,
    season_year: int | None = None,
    snapshot: str | None = None,
) -> dict[str, Any]:
    """Refresh every open game of `season_year` into a new snapshot dir. Returns the manifest; when
    nothing is open no directory is written and `out_dir` is None. Loading is the caller's job."""
    season_year = season_year or settings.season_year
    opened = open_games(conn, season_year)
    if not opened:
        log.info("refresh %d: no open games in the database", season_year)
        return {"season_year": season_year, "open_before": 0, "out_dir": None}

    collector = ESPNCollector(settings, snapshot or refresh_snapshot_tag(), core_odds=True)
    open_ids = [g["game_id"] for g in opened]
    season_weeks = sorted({(g["season_type"], g["week"]) for g in opened if g["season_type"] and g["week"]})
    log.info(
        "refresh %d: %d open games across weeks %s -> %s", season_year, len(opened), season_weeks, collector.out_dir
    )
    async with ESPNClient(settings, raw_dir=collector.raw_dir) as client:
        scoreboard = await collector.collect_scoreboard_for_games(client, season_year, season_weeks, open_ids)
        plan = plan_refresh(scoreboard["games"], open_ids)
        collected = await collector.collect_games(client, plan["collect"]) if plan["collect"] else []

    fresh = {g["game_id"]: g for g in scoreboard["games"]}
    fetched = {r["game_id"] for r in collected}
    still_open = [
        {
            "game_id": g["game_id"],
            "short_name": g["short_name"],
            "kickoff_utc": fresh.get(g["game_id"], g)["kickoff_utc"],
            "status_name": fresh.get(g["game_id"], g)["status_name"],
        }
        for g in opened
        if fresh.get(g["game_id"], g).get("status_state") != TERMINAL_STATE
    ]
    manifest = {
        "meta": _meta(collector.snapshot_utc, collector.snapshot),
        "out_dir": str(collector.out_dir),
        "season_year": season_year,
        "weeks": [list(w) for w in season_weeks],
        "open_before": len(opened),
        "scoreboard_games": len(scoreboard["games"]),
        "collected": len(collected),
        "collect_failed": [gid for gid in plan["collect"] if gid not in fetched],
        "scoreboard_only": len(plan["scoreboard_only"]),
        "missing_from_scoreboard": plan["missing"],
        "resolved": len(opened) - len(still_open),
        "still_open": still_open,
    }
    write_json(collector.out_dir / "manifest.json", manifest)
    log.info(
        "refresh %d: %d collected, %d resolved, %d still open, %d missing",
        season_year,
        len(collected),
        manifest["resolved"],
        len(still_open),
        len(plan["missing"]),
    )
    return manifest
