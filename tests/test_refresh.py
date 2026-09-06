"""Refresh of open games: open-set query, scoreboard plan, snapshot naming, latest-state views."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ncaaf_clv import storage
from ncaaf_clv.config import Settings
from ncaaf_clv.espn.refresh import open_games, plan_refresh, refresh_snapshot_tag
from ncaaf_clv.weather.collect import latest_espn_snapshot


def _game(
    game_id: str,
    snapshot_utc: str,
    state: str,
    name: str = "STATUS_SCHEDULED",
    season_year: int = 2026,
    kickoff: str = "2026-09-05T20:00:00Z",
) -> dict:
    return {
        "game_id": game_id,
        "snapshot_utc": snapshot_utc,
        "short_name": f"G{game_id}",
        "season_year": season_year,
        "season_type": 2,
        "week": 1,
        "kickoff_utc": kickoff,
        "status_name": name,
        "status_state": state,
    }


def test_open_games_uses_latest_row_per_game(tmp_path: Path) -> None:
    conn = storage.connect(tmp_path / "t.db")
    storage.insert_rows(
        conn,
        "espn_games",
        [
            _game("1", "2026-09-05T20:00:00Z", "pre"),  # scheduled -> open
            _game("2", "2026-09-05T20:00:00Z", "in", "STATUS_IN_PROGRESS"),  # live at first snapshot ...
            _game("2", "2026-09-05T23:00:00Z", "post", "STATUS_FINAL"),  # ... final at the latest -> closed
            _game("3", "2026-09-05T20:00:00Z", "post", "STATUS_CANCELED"),  # terminal without a final -> closed
            _game("4", "2026-09-05T20:00:00Z", "pre", season_year=2025),  # other season -> ignored
            _game("5", "2026-09-05T23:00:00Z", "in", "STATUS_DELAYED"),  # delayed -> open
        ],
    )
    opened = open_games(conn, 2026)
    assert [g["game_id"] for g in opened] == ["1", "5"]
    assert opened[0]["week"] == 1 and opened[0]["season_type"] == 2
    conn.close()


def test_plan_refresh_splits_by_fresh_scoreboard() -> None:
    scoreboard = [
        {"game_id": "1", "status_state": "pre"},
        {"game_id": "5", "status_state": "post"},
        {"game_id": "6", "status_state": "in"},
        {"game_id": "9", "status_state": "post"},  # not open: ignored
    ]
    assert plan_refresh(scoreboard, ["1", "5", "6", "7"]) == {
        "collect": ["5", "6"],
        "scoreboard_only": ["1"],
        "missing": ["7"],
    }


def test_refresh_snapshot_tag_prefix() -> None:
    assert refresh_snapshot_tag(datetime(2026, 9, 6, 1, 2, 3, tzinfo=UTC)) == "refresh-20260906T010203Z"


def test_latest_view_and_game_lines_do_not_fan_out(tmp_path: Path) -> None:
    conn = storage.connect(tmp_path / "t.db")
    storage.insert_rows(
        conn,
        "espn_games",
        [
            _game("1", "2026-09-05T20:00:00Z", "in", "STATUS_IN_PROGRESS"),
            _game("1", "2026-09-05T23:00:00Z", "post", "STATUS_FINAL"),
        ],
    )
    storage.insert_rows(
        conn, "overtime_odds", [{"ot_game_num": "77", "period_key": "game", "snapshot_utc": "2026-09-05T18:00:00Z"}]
    )
    storage.insert_rows(
        conn, "game_map", [{"ot_game_num": "77", "snapshot_utc": "2026-09-05T18:00:00Z", "espn_game_id": "1"}]
    )
    assert conn.execute("SELECT status_name FROM v_espn_games_latest WHERE game_id = '1'").fetchall() == [
        ("STATUS_FINAL",)
    ]
    assert conn.execute("SELECT status_name FROM v_game_lines").fetchall() == [("STATUS_FINAL",)]
    conn.close()


def test_latest_espn_snapshot_skips_refresh_and_backfill_dirs(tmp_path: Path) -> None:
    root = tmp_path / "processed" / "espn"
    for name in ("20260901T120000Z", "backfill-2025", "refresh-20260906T010203Z"):
        (root / name).mkdir(parents=True)
        (root / name / "scoreboard.json").write_text("{}", encoding="utf-8")
    assert latest_espn_snapshot(Settings(data_dir=tmp_path)) == "20260901T120000Z"
