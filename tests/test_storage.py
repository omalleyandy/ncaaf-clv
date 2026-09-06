"""SQLite loader: only real programs are loaded into espn_teams."""

from __future__ import annotations

import json
from pathlib import Path

from ncaaf_clv import storage


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _team(team_id: str, name: str, is_program: bool) -> dict:
    return {
        "source": "espn",
        "snapshot_utc": "2026-09-05T00:00:00Z",
        "team_id": team_id,
        "display_name": name,
        "is_program": is_program,
    }


def test_load_espn_snapshot_skips_non_program_teams(tmp_path: Path) -> None:
    espn_dir = tmp_path / "espn"
    _write(espn_dir / "scoreboard.json", {"games": [], "game_teams": [], "espn_odds": []})
    _write(
        espn_dir / "teams.json",
        {
            "meta": {"snapshot_utc": "2026-09-05T00:00:00Z"},
            "teams": [
                _team("2579", "South Carolina Gamecocks", True),
                _team("3198", "SOUTH FLORIDA STARS", False),
                _team("3146", "East All-Stars", False),
            ],
            "conferences": [],
        },
    )

    conn = storage.connect(tmp_path / "test.db")
    counts = storage.load_espn_snapshot(conn, espn_dir)

    assert counts["espn_teams"] == 1
    rows = conn.execute("SELECT team_id, is_program FROM espn_teams").fetchall()
    assert rows == [("2579", 1)]
    conn.close()


def test_load_espn_snapshot_without_team_dimension(tmp_path: Path) -> None:
    """Refresh snapshots carry only the open games' scoreboard (and their game files): no teams.json."""
    espn_dir = tmp_path / "refresh-20260906T010203Z"
    _write(
        espn_dir / "scoreboard.json",
        {
            "games": [{"game_id": "1", "snapshot_utc": "2026-09-06T01:02:03Z", "status_name": "STATUS_FINAL"}],
            "game_teams": [],
            "espn_odds": [],
        },
    )

    conn = storage.connect(tmp_path / "test.db")
    counts = storage.load_espn_snapshot(conn, espn_dir)

    assert counts["espn_games"] == 1 and "espn_teams" not in counts
    assert conn.execute("SELECT COUNT(*) FROM espn_teams").fetchone() == (0,)
    conn.close()
