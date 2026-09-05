from ncaaf_clv.cli import parse_int_ranges
from ncaaf_clv.espn.collect import assign_conference
from ncaaf_clv.espn.parse import parse_core_odds, parse_core_season_team

SNAP = "2026-09-05T21:00:00Z"
CORE = "http://sports.core.api.espn.com/v2/sports/football/leagues/college-football"


def _core_team(tid, group, all_star=False):
    return {
        "id": tid,
        "uid": f"s:20~l:23~t:{tid}",
        "slug": f"team-{tid}",
        "location": f"Loc {tid}",
        "name": "Tigers",
        "nickname": f"Loc {tid}",
        "abbreviation": f"T{tid}",
        "displayName": f"Loc {tid} Tigers",
        "shortDisplayName": f"Loc {tid}",
        "color": "000000",
        "isActive": True,
        "isAllStar": all_star,
        "logos": [{"href": "https://a.espncdn.com/x.png"}],
        "groups": {"$ref": f"{CORE}/seasons/2023/types/3/groups/{group}?lang=en"},
    }


def test_parse_core_season_team_and_conference_assignment():
    confs = {"8": {"group_id": "8", "name": "SEC", "abbreviation": "sec"}}
    parent_of = {"7": "8"}  # SEC West -> SEC
    division = assign_conference(parse_core_season_team(_core_team("2", "7"), SNAP), confs, parent_of, 80)
    assert division["team_id"] == "2" and division["nickname"] == "Tigers" and division["snapshot_utc"] == SNAP
    assert division["conference_group_id"] == "8" and division["conference_name"] == "SEC"
    assert division["division_group_id"] == "7" and division["parent_group_id"] == "8"
    assert division["is_program"] is True and "group_id" not in division and "is_all_star" not in division

    direct = assign_conference(parse_core_season_team(_core_team("3", "8"), SNAP), confs, parent_of, 80)
    assert direct["conference_group_id"] == "8" and direct["division_group_id"] is None
    assert direct["parent_group_id"] == "80" and direct["fbs_group_id"] == "80"

    all_star = assign_conference(parse_core_season_team(_core_team("9", "8", all_star=True), SNAP), confs, {}, 80)
    assert all_star["is_program"] is False and all_star["conference_group_id"] == "8"

    unknown = assign_conference(parse_core_season_team(_core_team("4", "999"), SNAP), confs, {}, 80)
    assert unknown["is_program"] is False and unknown["conference_group_id"] is None

    no_group = parse_core_season_team({"id": "5", "location": "Loc"}, SNAP)
    assert no_group["group_id"] is None


def _price(american):
    return {"american": american, "alternateDisplayValue": american}


def test_parse_core_odds_open_close_and_current_only():
    espn_bet = {
        "provider": {"id": "58", "name": "ESPN BET"},
        "details": "DEL -2.5",
        "spread": -2.5,
        "overUnder": 63.5,
        "overOdds": -110.0,
        "underOdds": -110.0,
        "homeTeamOdds": {
            "favorite": True,
            "moneyLine": -150,
            "spreadOdds": -125.0,
            "open": {"pointSpread": _price("-2.5"), "spread": _price("-110"), "moneyLine": _price("-135")},
            "close": {"pointSpread": _price("-2.5"), "spread": _price("-125"), "moneyLine": _price("-150")},
        },
        "awayTeamOdds": {
            "favorite": False,
            "moneyLine": 130,
            "spreadOdds": 105.0,
            "open": {"pointSpread": _price("+2.5"), "moneyLine": _price("+115")},
        },
        "open": {"over": _price("+100"), "under": _price("-120"), "total": _price("62.5")},
        "close": {"over": _price("-110"), "under": _price("-110"), "total": _price("63.5")},
    }
    draftkings_2021 = {
        "provider": {"id": "40", "name": "DraftKings"},
        "details": "NEB -7",
        "spread": 7.0,
        "overUnder": 52.5,
        "homeTeamOdds": {"favorite": False, "current": {"pointSpread": _price("+7"), "moneyLine": _price("+200")}},
        "awayTeamOdds": {"favorite": True, "current": {"pointSpread": _price("-7"), "moneyLine": _price("EVEN")}},
        "current": {"over": _price("-110"), "under": _price("-110"), "total": _price("52.5")},
    }
    rows = parse_core_odds([espn_bet, draftkings_2021], "401", "H", "A", SNAP)
    assert [r["provider_id"] for r in rows] == ["58", "40"]
    eb, dk = rows
    assert eb["favorite_team_id"] == "H" and eb["home_spread"] == -2.5 and eb["away_spread"] == 2.5
    assert eb["spread_magnitude"] == 2.5 and eb["total"] == 63.5
    assert eb["home_ml"] == -150 and eb["away_ml"] == 130
    assert eb["home_spread_price"] == -125 and eb["away_spread_price"] == 105
    assert eb["open_home_spread"] == -2.5 and eb["open_total"] == 62.5
    assert eb["open_home_ml"] == -135 and eb["open_away_ml"] == 115
    assert eb["close_home_spread"] == -2.5 and eb["close_total"] == 63.5
    assert eb["over_price"] == -110 and eb["under_price"] == -110

    assert dk["favorite_team_id"] == "A" and dk["home_spread"] == 7.0 and dk["away_spread"] == -7.0
    assert dk["home_ml"] == 200 and dk["away_ml"] == 100  # EVEN -> +100
    assert dk["over_price"] == -110 and dk["under_price"] == -110
    assert dk["open_home_spread"] is None and dk["close_home_spread"] is None and dk["close_total"] is None
    assert parse_core_odds([], "401", "H", "A", SNAP) == []


def test_parse_int_ranges():
    assert parse_int_ranges("2021-2025") == [2021, 2022, 2023, 2024, 2025]
    assert parse_int_ranges("1,3,5-7, 7") == [1, 3, 5, 6, 7]
