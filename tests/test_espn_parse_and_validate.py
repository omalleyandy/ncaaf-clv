from ncaaf_clv.espn.parse import parse_boxscore, parse_espn_odds, parse_play_by_play, parse_scoreboard
from ncaaf_clv.validate.validator import build_report, check_overtime_internal

SNAP = "2026-09-05T20:00:00Z"


def _competitor(tid, side, score="0"):
    return {
        "id": tid,
        "homeAway": side,
        "score": score,
        "winner": False,
        "team": {"id": tid, "displayName": f"Team {tid}", "location": f"Loc {tid}"},
    }


def test_parse_scoreboard_and_odds_canonical_spread():
    odds = [
        {
            "provider": {"id": "100", "name": "DraftKings"},
            "details": "H -7.5",
            "spread": -7.5,
            "overUnder": 50.5,
            "homeTeamOdds": {"favorite": True, "team": {"id": "1"}, "moneyLine": -300},
            "awayTeamOdds": {"favorite": False, "team": {"id": "2"}, "moneyLine": 240},
            "pointSpread": {
                "home": {"open": {"line": "-6.5", "odds": "-110"}, "close": {"line": "-7.5", "odds": "-105"}},
                "away": {"close": {"odds": "-115"}},
            },
            "total": {
                "over": {"open": {"line": "o49.5"}, "close": {"line": "o50.5", "odds": "-110"}},
                "under": {"close": {"odds": "-110"}},
            },
            "moneyline": {"home": {"open": {"odds": "-280"}}, "away": {"open": {"odds": "+230"}}},
        }
    ]
    payload = {
        "events": [
            {
                "id": "401",
                "uid": "u",
                "name": "B at A",
                "shortName": "B @ A",
                "date": "2026-09-05T23:30Z",
                "season": {"year": 2026, "type": 2},
                "week": {"number": 1},
                "competitions": [
                    {
                        "neutralSite": False,
                        "competitors": [_competitor("1", "home"), _competitor("2", "away")],
                        "status": {"type": {"id": "1", "name": "STATUS_SCHEDULED", "state": "pre"}},
                        "odds": odds,
                    }
                ],
            }
        ]
    }
    out = parse_scoreboard(payload, SNAP)
    g = out["games"][0]
    assert g["game_id"] == "401" and g["home_team_id"] == "1" and g["away_team_id"] == "2"
    assert g["kickoff_utc"] == "2026-09-05T23:30:00Z" and g["snapshot_utc"] == SNAP
    assert len(out["game_teams"]) == 2
    o = out["espn_odds"][0]
    assert o["favorite_team_id"] == "1" and o["spread_magnitude"] == 7.5
    assert o["home_spread"] == -7.5 and o["away_spread"] == 7.5
    assert o["home_ml"] == -300 and o["away_ml"] == 240 and o["open_away_ml"] == 230
    assert o["open_home_spread"] == -6.5 and o["close_home_spread"] == -7.5 and o["open_total"] == 49.5
    assert parse_espn_odds([], "401", SNAP) == []


def test_parse_boxscore_and_pbp_ids():
    payload = {
        "header": {"id": "401", "competitions": [{"id": "401"}]},
        "boxscore": {
            "teams": [
                {
                    "team": {"id": "1"},
                    "homeAway": "home",
                    "statistics": [
                        {"name": "totalYards", "displayValue": "400", "value": 400.0, "label": "Total Yards"}
                    ],
                }
            ],
            "players": [
                {
                    "team": {"id": "1"},
                    "statistics": [
                        {
                            "name": "passing",
                            "keys": ["completions/passingAttempts", "passingYards"],
                            "labels": ["C/ATT", "YDS"],
                            "athletes": [
                                {
                                    "athlete": {"id": "9", "displayName": "QB One", "jersey": "7"},
                                    "stats": ["20/30", "250"],
                                }
                            ],
                        }
                    ],
                }
            ],
        },
        "drives": {
            "previous": [
                {
                    "id": "d1",
                    "team": {"id": "1"},
                    "result": "TD",
                    "isScore": True,
                    "yards": 75,
                    "offensivePlays": 6,
                    "start": {"period": {"number": 1}, "clock": {"displayValue": "15:00"}, "yardLine": 25},
                    "end": {"period": {"number": 1}, "clock": {"displayValue": "12:00"}, "yardLine": 100},
                    "plays": [
                        {
                            "id": "p1",
                            "sequenceNumber": "1",
                            "type": {"id": "5", "text": "Rush"},
                            "text": "run",
                            "awayScore": 0,
                            "homeScore": 0,
                            "period": {"number": 1},
                            "clock": {"displayValue": "15:00"},
                            "scoringPlay": False,
                            "statYardage": 5,
                            "start": {"down": 1, "distance": 10, "yardLine": 25, "team": {"id": "1"}},
                            "end": {"down": 2, "distance": 5, "yardLine": 30, "team": {"id": "1"}},
                            "teamParticipants": [{"id": "1", "type": "offense"}],
                        }
                    ],
                }
            ],
            "current": {"id": "d2", "team": {"id": "2"}, "plays": []},
        },
    }
    box = parse_boxscore(payload, SNAP)
    assert box["team_stats"][0]["value"] == 400.0 and box["team_stats"][0]["team_id"] == "1"
    keys = [(r["stat_key"], r["display_value"], r["value"]) for r in box["player_stats"]]
    assert keys == [("completions/passingAttempts", "20/30", None), ("passingYards", "250", 250.0)]
    assert all(r["player_id"] == "9" and r["game_id"] == "401" for r in box["player_stats"])
    pbp = parse_play_by_play(payload, SNAP)
    assert [d["drive_id"] for d in pbp["drives"]] == ["d1", "d2"]
    p = pbp["plays"][0]
    assert p["play_id"] == "p1" and p["drive_id"] == "d1" and p["offense_team_id"] == "1" and p["start_down"] == 1


def _odds_row(**over):
    base = {
        "ot_game_num": "1",
        "period_key": "game",
        "spread_magnitude": 7.0,
        "favorite_side": "home",
        "home_ml": -300,
        "away_ml": 240,
        "total": 50.0,
        "away_spread_price": -110,
        "home_spread_price": -110,
        "over_price": -110,
        "under_price": -110,
        "home_team_total": 28.5,
        "away_team_total": 21.5,
        "kickoff_utc": "2026-09-05T23:30:00Z",
    }
    base.update(over)
    return base


def test_validator_flags_bad_favorite_and_prices():
    odds = [
        _odds_row(),
        _odds_row(
            period_key="1h",
            spread_magnitude=3.5,
            total=24.5,
            favorite_side=None,
            home_ml=None,
            away_ml=None,
            home_team_total=None,
            away_team_total=None,
            over_price=0,
        ),
    ]
    games = [{"ot_game_num": "1", "kickoff_utc": "2026-09-05T23:30:00Z"}]
    checks = {c.name: c for c in check_overtime_internal(odds, games)}
    assert not checks["ot_spread_favorite_consistent"].passed  # 1h has spread but no favourite
    assert not checks["ot_prices_in_range"].passed  # over_price 0
    assert checks["ot_team_totals_sum_to_total"].passed
    report = build_report(list(checks.values()), SNAP)
    assert report["meta"]["status"] == "FAIL" and report["meta"]["errors"] == 2
