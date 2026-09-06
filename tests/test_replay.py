"""Replay pages: play ordering, ball-spot repair, hold plays, win-probability carry, rendering, logos."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ncaaf_clv.common import write_json
from ncaaf_clv.config import Settings
from ncaaf_clv.replay.build import (
    DATA_TOKEN,
    badge_data_uri,
    build_replay_data,
    clock_seconds,
    default_snapshot,
    list_game_ids,
    logo_data_uri,
    render_replay,
    run_replay,
)

HOME, AWAY = "8", "2453"
PNG = b"\x89PNG\r\n\x1a\n" + b"fake"


def _play(play_id: str, period: int, clock: str, play_type: str, **kw) -> dict:
    base = {
        "play_id": play_id,
        "period": period,
        "clock": clock,
        "play_type": play_type,
        "offense_team_id": HOME,
        "text": "",
        "away_score": 0,
        "home_score": 0,
        "scoring_play": False,
        "is_penalty": False,
        "is_turnover": False,
        "start_yard_line": 25,
        "start_possession_team_id": HOME,
        "end_yard_line": 25,
        "end_possession_team_id": HOME,
        "start_down": 1,
        "start_distance": 10,
        "end_down": 1,
        "end_distance": 10,
        "down_distance_text": None,
        "drive_id": "d1",
    }
    base.update(kw)
    return base


def _team(team_id: str, abbr: str, location: str, color: str, alt: str | None, line: list[int]) -> dict:
    return {
        "team_id": team_id,
        "home_away": "home" if team_id == HOME else "away",
        "score": sum(line),
        "linescores": [float(x) for x in line],
        "team_location": location,
        "team_nickname": "Team",
        "team_abbreviation": abbr,
        "team_display_name": f"{location} Team",
        "team_color": color,
        "team_alternate_color": alt,
        "team_logo": f"https://a.espncdn.com/i/teamlogos/ncaa/500/{team_id}.png",
    }


@pytest.fixture
def game_dir(tmp_path: Path) -> Path:
    """A tiny game with ESPN's quirks: a timeout listed after the play it preceded, a penalty whose
    end_yard_line is 0, a touchdown, a period end, and win probability missing for some plays."""
    gdir = tmp_path / "data" / "processed" / "espn" / "refresh-20260906T045500Z" / "games" / "401"
    plays = [
        _play(
            "p1",
            1,
            "15:00",
            "Kickoff",
            start_yard_line=65,
            start_possession_team_id=AWAY,
            end_yard_line=25,
            text="(15:00) #37 kickoff 65 yards to the ARK00, Touchback",
            down_distance_text="1st & 10 at ARK 25",
        ),
        _play(
            "p2",
            1,
            "14:50",
            "Pass Reception",
            start_yard_line=25,
            end_yard_line=41,
            text="(15:00) pass complete to <b>#3</b> for 16 yards, 1ST DOWN",
            down_distance_text="1st & 10 at ARK 41",
        ),
        # listed after p2 but its clock says it came first
        _play("p3", 1, "14:58", "Timeout", text="Timeout Arkansas, clock 14:58", start_down=0, start_distance=0),
        _play(
            "p4",
            1,
            "14:20",
            "Penalty",
            start_yard_line=41,
            end_yard_line=0,
            is_penalty=True,
            text="PENALTY ARK False Start 5 yards from ARK41 to ARK36. NO PLAY",
            end_distance=15,
        ),
        _play(
            "p5",
            1,
            "13:50",
            "Passing Touchdown",
            start_yard_line=36,
            end_yard_line=100,
            scoring_play=True,
            home_score=7,
            text="(14:20) pass complete deep left, TOUCHDOWN, clock 13:50 #30 kick attempt good.",
            end_down=0,
            end_distance=0,
        ),
        _play("p6", 1, "0:00", "End Period", start_yard_line=35, end_yard_line=35, text="End of 1st quarter."),
    ]
    write_json(
        gdir / "play_by_play.json",
        {
            "meta": {"snapshot": "refresh-20260906T045500Z", "game_id": "401"},
            "drives": [
                {
                    "drive_id": "d1",
                    "drive_order": 1,
                    "team_id": HOME,
                    "description": "3 plays, 75 yards, 1:10",
                    "display_result": "Touchdown",
                    "start_period": 1,
                    "start_clock": "15:00",
                    "start_yard_line": 25,
                    "end_yard_line": 100,
                }
            ],
            "plays": plays,
        },
    )
    write_json(
        gdir / "gamecast.json",
        {
            "game": {
                "game_id": "401",
                "home_team_id": HOME,
                "away_team_id": AWAY,
                "kickoff_utc": "2026-09-05T20:15:00Z",
                "week": 1,
                "neutral_site": False,
                "venue_name": "Donald W. Reynolds Razorback Stadium",
                "venue_city": "Fayetteville",
                "venue_state": "AR",
                "attendance": 69178,
                "home_score": 7,
                "away_score": 0,
                "status_state": "post",
                "status_detail": "Final",
            },
            "teams": [
                _team(HOME, "ARK", "Arkansas", "a32136", "ffffff", [7, 0, 0, 0]),
                _team(AWAY, "UNA", "North Alabama", "000000", None, [0, 0, 0, 0]),
            ],
            "scoring_plays": [
                {
                    "play_id": "p5",
                    "team_id": HOME,
                    "period": 1,
                    "clock": "13:50",
                    "text": "TD pass",
                    "away_score": 0,
                    "home_score": 7,
                }
            ],
            "win_probability": [
                {"play_id": "d1", "seq": 0, "home_win_pct": 0.59},
                {"play_id": "p1", "seq": 1, "home_win_pct": 0.60},
                {"play_id": "p2", "seq": 2, "home_win_pct": 0.62},
                {"play_id": "p5", "seq": 3, "home_win_pct": 0.90},
            ],
            "espn_odds": [{"provider_name": "Draft Kings", "close_home_spread": -40.5, "close_total": 63.5}],
        },
    )
    return gdir


def test_clock_seconds() -> None:
    assert clock_seconds("15:00") == 900
    assert clock_seconds("0:36") == 36
    assert clock_seconds(None) == 0
    assert clock_seconds("junk") == 0


def test_plays_are_ordered_by_period_and_clock(game_dir: Path) -> None:
    data = build_replay_data(game_dir)
    assert [p["id"] for p in data["plays"]] == ["p1", "p3", "p2", "p4", "p5", "p6"]
    assert all(p["drv"] == 1 for p in data["plays"])


def test_hold_plays_inherit_the_previous_state(game_dir: Path) -> None:
    by_id = {p["id"]: p for p in build_replay_data(game_dir)["plays"]}
    timeout = by_id["p3"]
    assert (timeout["s"], timeout["e"]) == (25, 25)  # where the kickoff left the ball
    assert timeout["pos0"] == timeout["pos1"] == HOME
    assert (timeout["d1"], timeout["dist1"]) == (1, 10)
    assert timeout["dd"] is None
    end_period = by_id["p6"]
    assert (end_period["s"], end_period["e"]) == (100, 100)  # after the touchdown


def test_ball_spots_are_repaired_from_the_next_play(game_dir: Path) -> None:
    by_id = {p["id"]: p for p in build_replay_data(game_dir)["plays"]}
    assert by_id["p4"]["e"] == 36  # ESPN wrote 0; the next snap was at ARK 36
    assert by_id["p2"]["e"] == 41
    assert by_id["p5"]["e"] == 100  # touchdown into the away end zone
    assert by_id["p1"]["s"] == 65 and by_id["p1"]["e"] == 25


def test_snap_clock_and_text_prefix(game_dir: Path) -> None:
    by_id = {p["id"]: p for p in build_replay_data(game_dir)["plays"]}
    assert by_id["p5"]["snap"] == "14:20"
    assert by_id["p5"]["txt"].startswith("pass complete deep left")
    assert by_id["p4"]["snap"] is None


def test_win_probability_is_carried_forward(game_dir: Path) -> None:
    data = build_replay_data(game_dir)
    assert data["pregameWp"] == 0.59
    wp = {p["id"]: p["wp"] for p in data["plays"]}
    assert wp == {"p1": 0.60, "p3": 0.60, "p2": 0.62, "p4": 0.62, "p5": 0.90, "p6": 0.90}


def test_teams_title_and_note(game_dir: Path) -> None:
    data = build_replay_data(game_dir, {HOME: "data:image/png;base64,QUJD"})
    assert data["title"] == "North Alabama at Arkansas"
    assert data["game"]["date"] == "Sat, Sep 5, 2026 · 4:15 PM ET"
    home, away = data["teams"][HOME], data["teams"][AWAY]
    assert home["logo"] == "data:image/png;base64,QUJD"
    assert away["logo"].startswith("data:image/svg+xml;base64,")  # badge fallback
    assert (home["color"], home["alt"], away["alt"]) == ("a32136", "ffffff", None)
    assert home["line"] == [7, 0, 0, 0] and home["score"] == 7
    assert "ARK -40.5, total 63.5" in data["note"]
    assert "Final margin ARK by 7" in data["note"]
    assert "UNA lists #000000 with no alternate" in data["note"]
    assert data["scoring"][0]["id"] == "p5"


def test_render_inlines_data_and_escapes_script_terminators(game_dir: Path) -> None:
    html = render_replay(build_replay_data(game_dir))
    assert "<title>North Alabama at Arkansas Replay</title>" in html
    assert DATA_TOKEN not in html
    assert "<\\/b>" in html and "</b>" not in html.split("const D = ", 1)[1].split(";\n", 1)[0]
    payload = html.split("const D = ", 1)[1].split(";\n", 1)[0]
    assert json.loads(payload)["plays"][2]["id"] == "p2"  # the escape is transparent to a JSON parser


def test_build_requires_plays(tmp_path: Path) -> None:
    gdir = tmp_path / "g"
    write_json(gdir / "play_by_play.json", {"plays": []})
    write_json(gdir / "gamecast.json", {"game": {"home_team_id": HOME, "away_team_id": AWAY}, "teams": []})
    with pytest.raises(ValueError, match="no plays"):
        build_replay_data(gdir)


def test_logo_cache_and_fallback(tmp_path: Path) -> None:
    calls: list[str] = []

    def fetch(url: str) -> bytes | None:
        calls.append(url)
        return PNG if url.endswith("8.png") else None

    cache = tmp_path / "logos"
    first = logo_data_uri(HOME, "https://x/8.png", cache, fetch)
    assert first is not None and first.startswith("data:image/png;base64,")
    assert (cache / "8.png").read_bytes() == PNG
    assert logo_data_uri(HOME, "https://x/8.png", cache, fetch) == first
    assert calls == ["https://x/8.png"]  # second call served from the cache
    assert logo_data_uri(AWAY, "https://x/2453.png", cache, fetch) is None
    assert logo_data_uri(AWAY, "https://x/2453.png", cache, None) is None
    assert badge_data_uri("UNA", "000000").startswith("data:image/svg+xml;base64,")


def test_run_replay_writes_pages_and_manifest(game_dir: Path, tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    snapshot_dir = game_dir.parent.parent
    assert list_game_ids(snapshot_dir) == ["401"]
    assert default_snapshot(settings) == "refresh-20260906T045500Z"
    fetched: list[str] = []

    def fetch(url: str) -> bytes | None:
        fetched.append(url)
        return PNG

    manifest = run_replay(settings, "refresh-20260906T045500Z", ["401", "999"], fetcher=fetch)
    assert [w["game_id"] for w in manifest["written"]] == ["401"]
    assert manifest["written"][0]["plays"] == 6
    assert manifest["skipped"][0]["game_id"] == "999"
    out = Path(manifest["written"][0]["path"])
    assert out == tmp_path / "data" / "processed" / "replays" / "refresh-20260906T045500Z" / "401.html"
    html = out.read_text(encoding="utf-8")
    assert html.count("data:image/png;base64,") >= 2 and "data:image/svg+xml" not in html
    assert len(fetched) == 2
    assert (settings.raw_espn_dir / "logos" / "8.png").is_file()

    # --no-logos: nothing fetched, badges instead
    manifest = run_replay(settings, "refresh-20260906T045500Z", ["401"], out_dir=tmp_path / "out", logos=False)
    html = Path(manifest["written"][0]["path"]).read_text(encoding="utf-8")
    assert "data:image/svg+xml" in html
    assert len(fetched) == 2
