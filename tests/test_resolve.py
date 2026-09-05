from ncaaf_clv.resolve.game_join import join_overtime_to_espn
from ncaaf_clv.resolve.team_resolver import TeamResolver, normalize_name

TEAMS = [
    {
        "team_id": "145",
        "location": "Ole Miss",
        "nickname": "Rebels",
        "abbreviation": "MISS",
        "display_name": "Ole Miss Rebels",
        "short_display_name": "Ole Miss",
        "slug": "ole-miss-rebels",
        "is_program": True,
    },
    {
        "team_id": "62",
        "location": "Hawai'i",
        "nickname": "Rainbow Warriors",
        "abbreviation": "HAW",
        "display_name": "Hawai'i Rainbow Warriors",
        "short_display_name": "Hawai'i",
        "slug": "hawaii-rainbow-warriors",
        "is_program": True,
    },
    {
        "team_id": "58",
        "location": "South Florida",
        "nickname": "Bulls",
        "abbreviation": "USF",
        "display_name": "South Florida Bulls",
        "short_display_name": "South Florida",
        "slug": "south-florida-bulls",
        "is_program": True,
    },
    {
        "team_id": "3198",
        "location": "SOUTH FLORIDA",
        "nickname": "STARS",
        "abbreviation": "SFS",
        "display_name": "SOUTH FLORIDA STARS",
        "short_display_name": "South Florida",
        "slug": "x",
        "is_program": False,
    },
    {
        "team_id": "2623",
        "location": "Missouri State",
        "nickname": "Bears",
        "abbreviation": "MOST",
        "display_name": "Missouri State Bears",
        "short_display_name": "Missouri St",
        "slug": "missouri-state-bears",
        "is_program": True,
    },
    {
        "team_id": "245",
        "location": "Texas A&M",
        "nickname": "Aggies",
        "abbreviation": "TA&M",
        "display_name": "Texas A&M Aggies",
        "short_display_name": "Texas A&M",
        "slug": "texas-am-aggies",
        "is_program": True,
    },
]


def test_normalize_name():
    assert normalize_name("Hawai'i") == "hawaii"
    assert normalize_name("Missouri St") == "missouri state"
    assert normalize_name("Texas A&M") == "texas a and m"
    assert normalize_name("Sam Houston St.") == "sam houston state"


def test_resolver_location_alias_and_program_filter():
    r = TeamResolver(TEAMS, aliases={"Mississippi": "145", "mississippi": "145"})
    assert r.resolve("Mississippi").team_id == "145" and r.resolve("Mississippi").method == "alias"
    assert r.resolve("Hawaii").team_id == "62" and r.resolve("Hawaii").method == "location"
    assert r.resolve("Missouri St").team_id == "2623"  # St -> State via token map
    assert r.resolve("Texas A&M").team_id == "245"
    # all-star squad excluded, so no ambiguity
    assert r.resolve("South Florida").team_id == "58"
    unresolved = r.resolve("Nowhere Tech")
    assert not unresolved.resolved and unresolved.method == "unresolved"


def _espn_game(gid, away, home, kickoff):
    return {"game_id": gid, "away_team_id": away, "home_team_id": home, "kickoff_utc": kickoff}


def test_join_pair_swapped_and_window():
    r = TeamResolver(TEAMS)
    espn = [
        _espn_game("1", "62", "145", "2026-09-05T23:30:00Z"),  # Hawaii @ Ole Miss
        _espn_game("2", "58", "2623", "2026-09-05T20:00:00Z"),  # USF @ Missouri State (neutral, swapped on book)
        _espn_game("3", "245", "62", "2026-09-12T20:00:00Z"),  # a week away
    ]
    ot = [
        {
            "ot_game_num": "a",
            "away_team_raw": "Hawaii",
            "home_team_raw": "Ole Miss",
            "kickoff_utc": "2026-09-06T01:00:00Z",
        },
        {
            "ot_game_num": "b",
            "away_team_raw": "Missouri St",
            "home_team_raw": "South Florida",
            "kickoff_utc": "2026-09-05T20:00:00Z",
        },
        {
            "ot_game_num": "c",
            "away_team_raw": "Texas A&M",
            "home_team_raw": "Hawaii",
            "kickoff_utc": "2026-09-05T20:00:00Z",
        },
        {
            "ot_game_num": "d",
            "away_team_raw": "Nowhere",
            "home_team_raw": "Hawaii",
            "kickoff_utc": "2026-09-05T20:00:00Z",
        },
    ]
    out = join_overtime_to_espn(ot, espn, r, window_hours=12, snapshot_utc="s")
    by = {m["ot_game_num"]: m for m in out["game_map"]}
    assert by["a"]["espn_game_id"] == "1" and by["a"]["method"] == "pair" and by["a"]["kickoff_delta_minutes"] == 90.0
    assert by["b"]["espn_game_id"] == "2" and by["b"]["method"] == "pair_swapped" and by["b"]["orientation_swapped"]
    assert by["c"]["method"] == "pair_match_outside_window"
    assert by["d"]["method"] == "unresolved_team" and by["d"]["espn_game_id"] is None
    assert out["meta"]["matched"] == 2 and out["meta"]["teams_unresolved"] == ["Nowhere"]
