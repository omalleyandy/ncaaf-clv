from ncaaf_clv.overtime.parse import normalize_line, parse_overtime_dump

TZ = "America/New_York"


def _line(**over):
    base = {
        "GameNum": 115155947,
        "ParentGameNum": None,
        "Team1ID": "Tulane",
        "Team2ID": "Duke",
        "Team1RotNum": 167,
        "Team2RotNum": 168,
        "GameDateTime": "/Date(1788642001000)/",
        "GameDateTimeString": "09/05/2026 17:00",
        "PeriodNumber": 0,
        "PeriodDescription": "Game",
        "FavoredTeamID": "Duke",
        "Spread": -9,
        "SpreadAdj1": -108,
        "SpreadAdj2": -112,
        "TotalPoints": 52,
        "TtlPtsAdj1": -110,
        "TtlPtsAdj2": -110,
        "MoneyLine1": 305,
        "MoneyLine2": -365,
        "Team1TotalPoints": 21.5,
        "Team1TtlPtsAdj1": -105,
        "Team1TtlPtsAdj2": -125,
        "Team2TotalPoints": 30.5,
        "Team2TtlPtsAdj1": -115,
        "Team2TtlPtsAdj2": -115,
        "LineSeq": 0,
        "Status": "O",
        "PeriodWagerCutoff": "/Date(1788642000000)/",
    }
    base.update(over)
    return base


def test_home_favorite_spread_signs_and_prices():
    r = normalize_line(_line(), "snap", "cap", TZ)
    assert r["favorite_side"] == "home"
    assert r["spread_magnitude"] == 9.0
    assert r["home_spread"] == -9.0 and r["away_spread"] == 9.0
    assert r["away_spread_price"] == -108 and r["home_spread_price"] == -112
    assert r["away_ml"] == 305 and r["home_ml"] == -365
    assert r["over_price"] == -110 and r["under_price"] == -110
    assert r["away_team_total"] == 21.5 and r["home_team_total"] == 30.5


def test_away_favorite_and_pickem():
    away = normalize_line(_line(FavoredTeamID="Tulane", Spread=-3), "s", None, TZ)
    assert away["favorite_side"] == "away"
    assert away["home_spread"] == 3.0 and away["away_spread"] == -3.0
    pk = normalize_line(_line(FavoredTeamID="Tulane", Spread=0), "s", None, TZ)
    assert pk["spread_magnitude"] == 0.0 and pk["home_spread"] == 0.0 and pk["away_spread"] == 0.0


def test_eastern_local_time_converted_to_utc():
    r = normalize_line(_line(), "s", None, TZ)
    assert r["kickoff_utc"] == "2026-09-05T21:00:00Z"  # 17:00 EDT
    assert r["wager_cutoff_utc"] == "2026-09-05T21:00:00Z"


def test_epoch_fallback_when_string_missing():
    r = normalize_line(_line(GameDateTimeString=None), "s", None, TZ)
    assert r["kickoff_utc"] == "2026-09-05T21:00:01Z"


def test_period_keys_including_game_period_zero():
    assert normalize_line(_line(PeriodNumber=0), "s", None, TZ)["period_key"] == "game"
    assert normalize_line(_line(PeriodNumber=1), "s", None, TZ)["period_key"] == "1h"
    assert normalize_line(_line(PeriodNumber=3), "s", None, TZ)["period_key"] == "1q"


def test_parse_dump_compact_and_expanded_shapes():
    line = _line()
    expanded = parse_overtime_dump(
        {"captured_at": "2026-09-05T20:21:47Z", "lines": [line, _line(PeriodNumber=1, PeriodDescription="1st Half")]},
        TZ,
    )
    cols = list(line.keys())
    compact = parse_overtime_dump(
        {"captured_at": "2026-09-05T20:21:47Z", "cols": cols, "rows": [[line[c] for c in cols]]}, TZ
    )
    assert expanded["meta"]["line_count"] == 2 and expanded["meta"]["game_count"] == 1
    assert expanded["games"][0]["periods"] == ["game", "1h"]
    assert compact["odds"][0]["ot_game_num"] == "115155947"
    assert compact["odds"][0]["snapshot_utc"] == "2026-09-05T20:21:47Z"
