"""SQLite loader for processed JSON snapshots.

Rows are inserted with INSERT OR REPLACE keyed on the table's primary key (which always
includes snapshot_utc), so re-loading a snapshot is idempotent. Extra JSON keys not present
in the table are ignored; list/dict values are JSON-encoded.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ncaaf_clv.common import read_json

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _coerce(value: Any) -> Any:
    if isinstance(value, list | dict):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return int(value)
    return value


def insert_rows(conn: sqlite3.Connection, table: str, rows: Iterable[dict[str, Any]]) -> int:
    cols = _columns(conn, table)
    if not cols:
        raise ValueError(f"unknown table {table}")
    placeholders = ",".join("?" for _ in cols)
    sql = f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
    batch = [tuple(_coerce(r.get(c)) for c in cols) for r in rows]
    if batch:
        conn.executemany(sql, batch)
    return len(batch)


def load_espn_snapshot(conn: sqlite3.Connection, espn_dir: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    sb = read_json(espn_dir / "scoreboard.json")
    counts["espn_games"] = insert_rows(conn, "espn_games", sb["games"])
    counts["espn_game_teams"] = insert_rows(conn, "espn_game_teams", sb["game_teams"])
    counts["espn_odds"] = insert_rows(conn, "espn_odds", sb["espn_odds"])

    dims = read_json(espn_dir / "teams.json")
    snap = dims["meta"]["snapshot_utc"]
    counts["espn_teams"] = insert_rows(conn, "espn_teams", dims["teams"])
    counts["espn_conferences"] = insert_rows(
        conn, "espn_conferences", [{"snapshot_utc": snap, **c} for c in dims["conferences"]]
    )

    for gdir in sorted((espn_dir / "games").glob("*/")):
        if not (gdir / "gamecast.json").exists():
            continue
        gc = read_json(gdir / "gamecast.json")
        counts["espn_game_teams"] += insert_rows(conn, "espn_game_teams", gc["teams"])
        counts["espn_odds"] += insert_rows(conn, "espn_odds", gc["espn_odds"])
        counts["espn_scoring_plays"] = counts.get("espn_scoring_plays", 0) + insert_rows(
            conn, "espn_scoring_plays", gc["scoring_plays"]
        )
        counts["espn_game_leaders"] = counts.get("espn_game_leaders", 0) + insert_rows(
            conn, "espn_game_leaders", gc["leaders"]
        )
        counts["espn_win_probability"] = counts.get("espn_win_probability", 0) + insert_rows(
            conn, "espn_win_probability", gc["win_probability"]
        )
        box = read_json(gdir / "boxscore.json")
        counts["espn_boxscore_team_stats"] = counts.get("espn_boxscore_team_stats", 0) + insert_rows(
            conn, "espn_boxscore_team_stats", box["team_stats"]
        )
        counts["espn_boxscore_player_stats"] = counts.get("espn_boxscore_player_stats", 0) + insert_rows(
            conn, "espn_boxscore_player_stats", box["player_stats"]
        )
        pbp = read_json(gdir / "play_by_play.json")
        counts["espn_drives"] = counts.get("espn_drives", 0) + insert_rows(conn, "espn_drives", pbp["drives"])
        counts["espn_plays"] = counts.get("espn_plays", 0) + insert_rows(conn, "espn_plays", pbp["plays"])

    for tdir in sorted((espn_dir / "teams").glob("*/")):
        if not (tdir / "statistics.json").exists():
            continue
        counts["espn_team_season_stats"] = counts.get("espn_team_season_stats", 0) + insert_rows(
            conn, "espn_team_season_stats", read_json(tdir / "statistics.json")["statistics"]
        )
        counts["espn_team_schedule"] = counts.get("espn_team_schedule", 0) + insert_rows(
            conn, "espn_team_schedule", read_json(tdir / "schedule.json")["schedule"]
        )
        roster = read_json(tdir / "roster.json")
        counts["espn_roster"] = counts.get("espn_roster", 0) + insert_rows(conn, "espn_roster", roster["players"])
        counts["espn_coaches"] = counts.get("espn_coaches", 0) + insert_rows(conn, "espn_coaches", roster["coaches"])
    conn.commit()
    return counts


def load_overtime_snapshot(conn: sqlite3.Connection, odds_json: Path) -> int:
    payload = read_json(odds_json)
    n = insert_rows(conn, "overtime_odds", payload["odds"])
    conn.commit()
    return n


def load_join(conn: sqlite3.Connection, join_json: Path) -> dict[str, int]:
    payload = read_json(join_json)
    counts = {
        "game_map": insert_rows(conn, "game_map", payload["game_map"]),
        "team_map": insert_rows(conn, "team_map", payload["team_map"]),
    }
    conn.commit()
    return counts


def load_validation(conn: sqlite3.Connection, report_json: Path) -> int:
    payload = read_json(report_json)
    snap = payload["meta"]["snapshot_utc"]
    n = insert_rows(conn, "validation_checks", [{"snapshot_utc": snap, **c} for c in payload["checks"]])
    conn.commit()
    return n
