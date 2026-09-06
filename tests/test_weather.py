from datetime import UTC, datetime, timedelta, timezone

import pytest

from ncaaf_clv.config import SOURCE_ACCUWEATHER_FORECAST, SOURCE_ACCUWEATHER_OBSERVED
from ncaaf_clv.weather.collect import MODE_FORECAST, MODE_OBSERVED, games_in_scope
from ncaaf_clv.weather.parse import (
    WINDOW_STATUS_FORECAST,
    WINDOW_STATUS_INDOOR,
    expected_hours,
    game_window,
    parse_aw_datetime,
    parse_game_hours,
    reachable_hours,
    summarize_game_window,
)
from ncaaf_clv.weather.stadiums import (
    _loc_fields,
    canonical_key,
    clean_poi_query,
    is_indoor_venue,
    merge_venues,
    pick_location,
    poi_queries,
    venues_from_games,
)
from ncaaf_clv.weather.validate import validate_weather

SNAP = "2026-09-05T18:00:00Z"  # 14:00 ET
KICKOFF = "2026-09-05T23:30:00Z"  # 19:30 ET
PRE, DUR = 1.0, 3.5

GAME = {
    "game_id": "401756900",
    "home_team_id": "61",
    "away_team_id": "2",
    "kickoff_utc": KICKOFF,
    "time_valid": True,
    "venue_id": "3917",
    "venue_name": "Sanford Stadium",
    "venue_city": "Athens",
    "venue_state": "GA",
    "venue_country": "USA",
    "venue_indoor": False,
    "season_year": 2026,
}
DOME_GAME = {
    **GAME,
    "game_id": "401756901",
    "home_team_id": "183",
    "away_team_id": "150",
    "venue_id": "1964",
    "venue_name": "JMA Wireless Dome",
    "venue_city": "Syracuse",
    "venue_state": "NY",
    "venue_indoor": True,
}
LOCATION = {
    "venue_id": "3917",
    "location_key": "53561_POI",
    "is_indoor": False,
    "admin_area": "GA",
    "country_code": "US",
}


def _hour(
    local_hour: int, temp: float = 80.0, precip: int = 20, wind: float = 6.0, gust: float | None = 9.0, day: int = 5
):
    """One AccuWeather hourly item at `local_hour` ET (UTC-4) on 2026-09-<day>; DateTime and epoch agree."""
    local_dt = datetime(2026, 9, day, local_hour, 0, tzinfo=timezone(timedelta(hours=-4)))
    local = local_dt.isoformat()
    epoch = int(local_dt.timestamp())
    return {
        "DateTime": local,
        "EpochDateTime": epoch,
        "WeatherIcon": 15,
        "IconPhrase": "Thunderstorms",
        "HasPrecipitation": precip > 0,
        "PrecipitationType": "Rain" if precip > 0 else None,
        "PrecipitationIntensity": "Light" if precip > 0 else None,
        "IsDaylight": local_hour < 20,
        "Temperature": {"Value": temp, "Unit": "F", "UnitType": 18},
        "RealFeelTemperature": {"Value": temp + 4, "Unit": "F", "UnitType": 18, "Phrase": "Hot"},
        "RealFeelTemperatureShade": {"Value": temp + 2, "Unit": "F", "UnitType": 18},
        "WetBulbTemperature": {"Value": 72.0, "Unit": "F", "UnitType": 18},
        "DewPoint": {"Value": 70.0, "Unit": "F", "UnitType": 18},
        "Wind": {
            "Speed": {"Value": wind, "Unit": "mi/h"},
            "Direction": {"Degrees": 270, "Localized": "W", "English": "W"},
        },
        "WindGust": {"Speed": {"Value": gust, "Unit": "mi/h"}} if gust is not None else {},
        "RelativeHumidity": 55,
        "IndoorRelativeHumidity": 55,
        "Visibility": {"Value": 6.0, "Unit": "mi"},
        "Ceiling": {"Value": 9000.0, "Unit": "ft"},
        "UVIndex": 1,
        "UVIndexText": "Low",
        "PrecipitationProbability": precip,
        "ThunderstormProbability": 30,
        "RainProbability": precip,
        "SnowProbability": 0,
        "IceProbability": 0,
        "TotalLiquid": {"Value": 0.05, "Unit": "in"},
        "Rain": {"Value": 0.05, "Unit": "in"},
        "Snow": {"Value": 0.0, "Unit": "in"},
        "Ice": {"Value": 0.0, "Unit": "in"},
        "CloudCover": 98,
        "Evapotranspiration": {"Value": 0.01, "Unit": "in"},
        "SolarIrradiance": {"Value": 100.0, "Unit": "W/m²"},
        "MobileLink": "http://www.accuweather.com/en/us/sanford-stadium/30602/hourly-weather-forecast/53561_poi",
        "Link": "http://www.accuweather.com/en/us/sanford-stadium/30602/hourly-weather-forecast/53561_poi",
    }


# 12-hour horizon from the 14:00 ET snapshot: 14:00 .. 01:00 ET
PAYLOAD = [_hour(h) for h in range(14, 24)] + [_hour(0, day=6), _hour(1, day=6)]


def test_window_and_expected_hours():
    kickoff = datetime(2026, 9, 5, 23, 30, tzinfo=UTC)
    start, end = game_window(kickoff, PRE, DUR)
    assert start == datetime(2026, 9, 5, 22, 30, tzinfo=UTC) and end == datetime(2026, 9, 6, 3, 0, tzinfo=UTC)
    hours = expected_hours((start, end))
    # 22:00 (overlaps 22:30), 23:00, 00:00, 01:00, 02:00; 03:00 starts at the window end -> excluded
    assert [h.hour for h in hours] == [22, 23, 0, 1, 2]


def test_reachable_hours_skip_the_current_hour_for_forecasts():
    kickoff = datetime(2026, 9, 5, 21, 0, tzinfo=UTC)
    exp = expected_hours(game_window(kickoff, PRE, DUR))  # 20Z .. 00Z (5 hours)
    assert [h.hour for h in exp] == [20, 21, 22, 23, 0]
    snap = datetime(2026, 9, 5, 23, 14, 56, tzinfo=UTC)  # the API's first hour is 00:00Z, not 23:00Z
    assert [h.hour for h in reachable_hours(exp, snap, SOURCE_ACCUWEATHER_FORECAST)] == [0]
    on_the_hour = datetime(2026, 9, 5, 23, 0, tzinfo=UTC)
    assert [h.hour for h in reachable_hours(exp, on_the_hour, SOURCE_ACCUWEATHER_FORECAST)] == [23, 0]
    assert [h.hour for h in reachable_hours(exp, snap, SOURCE_ACCUWEATHER_OBSERVED)] == [20, 21, 22, 23]
    assert reachable_hours(exp, None, SOURCE_ACCUWEATHER_FORECAST) == exp
    w = summarize_game_window(
        {**GAME, "kickoff_utc": "2026-09-05T21:00:00Z"},
        LOCATION,
        [],
        "2026-09-05T23:14:56Z",
        PRE,
        DUR,
        WINDOW_STATUS_FORECAST,
    )
    assert w["hours_expected"] == 5 and w["hours_reachable"] == 1 and w["hours_present"] == 0


def test_parse_aw_datetime_prefers_epoch_and_handles_offsets():
    assert parse_aw_datetime("2026-09-05T19:00:00-04:00") == datetime(2026, 9, 5, 23, 0, tzinfo=UTC)
    assert parse_aw_datetime(None, 1_757_000_000) == datetime.fromtimestamp(1_757_000_000, tz=UTC)
    assert parse_aw_datetime("2026-09-05T19:00:00") is None  # naive local time is ambiguous -> rejected


def test_parse_game_hours_keeps_window_and_join_keys():
    rows = parse_game_hours(PAYLOAD, GAME, LOCATION, SNAP, PRE, DUR)
    # window 22:30Z-03:00Z; payload reaches 05:00Z, so 22Z, 23Z, 00Z, 01Z, 02Z are present
    assert [r["forecast_for_utc"] for r in rows] == [
        "2026-09-05T22:00:00Z",
        "2026-09-05T23:00:00Z",
        "2026-09-06T00:00:00Z",
        "2026-09-06T01:00:00Z",
        "2026-09-06T02:00:00Z",
    ]
    r = rows[0]
    assert r["source"] == SOURCE_ACCUWEATHER_FORECAST and r["snapshot_utc"] == SNAP
    assert r["game_id"] == "401756900" and r["home_team_id"] == "61" and r["away_team_id"] == "2"
    assert r["venue_id"] == "3917" and r["location_key"] == "53561_POI" and r["kickoff_utc"] == KICKOFF
    assert r["hour_offset_from_kickoff"] == -1.5 and r["lead_hours"] == 4.0 and r["in_game_window"] is True
    assert r["temp_f"] == 80.0 and r["real_feel_f"] == 84.0 and r["real_feel_shade_f"] == 82.0
    assert r["wind_speed_mph"] == 6.0 and r["wind_direction_deg"] == 270 and r["wind_direction_text"] == "W"
    assert r["wind_gust_mph"] == 9.0 and r["precip_probability"] == 20 and r["thunderstorm_probability"] == 30
    assert r["total_liquid_in"] == 0.05 and r["cloud_cover"] == 98 and r["ceiling_ft"] == 9000.0
    assert r["visibility_mi"] == 6.0 and r["uv_index"] == 1 and r["dew_point_f"] == 70.0
    assert r["pressure_inhg"] is None and r["link"].endswith("53561_poi")
    assert rows[1]["hour_offset_from_kickoff"] == -0.5 and rows[2]["hour_offset_from_kickoff"] == 0.5

    all_rows = parse_game_hours(PAYLOAD, GAME, LOCATION, SNAP, PRE, DUR, keep_all_hours=True)
    assert len(all_rows) == 12 and sum(1 for r in all_rows if r["in_game_window"]) == 5
    assert all_rows[0]["lead_hours"] == 0.0 and all_rows[0]["in_game_window"] is False


def test_parse_observed_rows_map_imperial_blocks():
    obs = [
        {
            "LocalObservationDateTime": "2026-09-05T19:53:00-04:00",
            "EpochTime": int(datetime(2026, 9, 5, 23, 53, tzinfo=UTC).timestamp()),
            "WeatherText": "Mostly cloudy",
            "WeatherIcon": 6,
            "IsDayTime": True,
            "HasPrecipitation": False,
            "PrecipitationType": None,
            "Temperature": {"Metric": {"Value": 30.0, "Unit": "C"}, "Imperial": {"Value": 86.0, "Unit": "F"}},
            "RealFeelTemperature": {"Imperial": {"Value": 92.0, "Unit": "F", "Phrase": "Hot"}},
            "DewPoint": {"Imperial": {"Value": 71.0}},
            "RelativeHumidity": 61,
            "Wind": {"Direction": {"Degrees": 225, "English": "SW"}, "Speed": {"Imperial": {"Value": 5.8}}},
            "WindGust": {"Speed": {"Imperial": {"Value": 8.1}}},
            "UVIndex": 2,
            "Visibility": {"Imperial": {"Value": 10.0}},
            "CloudCover": 70,
            "Ceiling": {"Imperial": {"Value": 25000.0}},
            "Pressure": {"Imperial": {"Value": 29.95}},
            "Precip1hr": {"Imperial": {"Value": 0.0}},
            "PrecipitationSummary": {"PastHour": {"Imperial": {"Value": 0.0}}},
        }
    ]
    rows = parse_game_hours(obs, GAME, LOCATION, "2026-09-06T12:00:00Z", PRE, DUR, SOURCE_ACCUWEATHER_OBSERVED)
    assert len(rows) == 1
    r = rows[0]
    assert r["forecast_for_utc"] == "2026-09-05T23:00:00Z"  # 19:53 local -> top of the hour
    assert r["source"] == SOURCE_ACCUWEATHER_OBSERVED and r["lead_hours"] == -13.0
    assert r["temp_f"] == 86.0 and r["real_feel_f"] == 92.0 and r["wind_speed_mph"] == 5.8
    assert r["pressure_inhg"] == 29.95 and r["precip_1hr_in"] == 0.0 and r["precip_probability"] is None
    assert r["icon_phrase"] == "Mostly cloudy" and r["wind_direction_text"] == "SW"


def test_summarize_game_window_aggregates_and_coverage():
    rows = parse_game_hours(PAYLOAD, GAME, LOCATION, SNAP, PRE, DUR)
    w = summarize_game_window(GAME, LOCATION, rows, SNAP, PRE, DUR, WINDOW_STATUS_FORECAST)
    assert w["hours_expected"] == 5 and w["hours_present"] == 5 and w["coverage_pct"] == 100.0
    assert w["lead_hours_at_kickoff"] == 5.5 and w["window_start_utc"] == "2026-09-05T22:30:00Z"
    assert w["temp_f_mean"] == 80.0 and w["temp_f_at_kickoff"] == 80.0 and w["wind_gust_mph_max"] == 9.0
    assert w["precip_probability_max"] == 20 and w["total_liquid_in_sum"] == 0.25 and w["hours_with_precipitation"] == 5
    assert w["icon_phrases"] == ["Thunderstorms"] and w["home_team_id"] == "61" and w["away_team_id"] == "2"

    # Horizon that stops before the window ends -> partial coverage is measurable
    short = parse_game_hours(PAYLOAD[:9], GAME, LOCATION, SNAP, PRE, DUR)  # 14:00..22:00 ET -> 22Z only... 18Z-02Z
    w2 = summarize_game_window(GAME, LOCATION, short, SNAP, PRE, DUR, WINDOW_STATUS_FORECAST)
    assert w2["hours_expected"] == 5 and w2["hours_present"] == 5  # 14..22 ET is 18Z..02Z: still covers 22Z-02Z
    shorter = parse_game_hours(PAYLOAD[:7], GAME, LOCATION, SNAP, PRE, DUR)  # 18Z..00Z
    w3 = summarize_game_window(GAME, LOCATION, shorter, SNAP, PRE, DUR, WINDOW_STATUS_FORECAST)
    assert w3["hours_present"] == 3 and w3["coverage_pct"] == 60.0

    dome = summarize_game_window(
        DOME_GAME, None, [], SNAP, PRE, DUR, WINDOW_STATUS_INDOOR, is_indoor=True, roof_type="fixed"
    )
    assert dome["status"] == WINDOW_STATUS_INDOOR and dome["hours_present"] == 0 and dome["temp_f_mean"] is None


def test_indoor_classification_and_poi_query():
    assert is_indoor_venue({"venue_id": "3687", "venue_name": "AT&T Stadium", "venue_indoor": True}) == (
        True,
        "retractable",
    )
    assert is_indoor_venue({"venue_id": "1964", "venue_name": "JMA Wireless Dome", "venue_indoor": True}) == (
        True,
        "fixed",
    )
    assert is_indoor_venue({"venue_id": "9999", "venue_name": "Kibbie Dome", "venue_indoor": False}) == (True, "fixed")
    assert is_indoor_venue({"venue_id": "3917", "venue_name": "Sanford Stadium", "venue_indoor": False}) == (
        False,
        None,
    )
    assert clean_poi_query("Tiger Stadium (LA)") == "Tiger Stadium"
    assert clean_poi_query("Memorial Stadium (Lincoln, NE)") == "Memorial Stadium"
    assert clean_poi_query("Ryan Field (1926)") == "Ryan Field"
    assert clean_poi_query("Sanford Stadium") == "Sanford Stadium"


def test_poi_queries_progressively_simplify_and_add_former_names():
    assert poi_queries({"venue_id": "3834", "venue_name": "Memorial Stadium (Lincoln, NE)"}) == ["Memorial Stadium"]
    assert poi_queries({"venue_id": "3887", "venue_name": "Donald W. Reynolds Razorback Stadium"}) == [
        "Donald W. Reynolds Razorback Stadium",
        "Razorback Stadium",
    ]
    assert poi_queries({"venue_id": "3696", "venue_name": "Zoeller Field at Dix Stadium"}) == [
        "Zoeller Field at Dix Stadium",
        "Dix Stadium",
    ]
    assert poi_queries({"venue_id": "3947", "venue_name": "Mountain America Stadium"}) == [
        "Mountain America Stadium",
        "America Stadium",
        "Sun Devil Stadium",
    ]


def test_canonical_key_and_location_fields_from_alias_record():
    assert canonical_key("3-53527_1_POI_AL") == ("53527_POI", "3-53527_1_POI_AL")
    assert canonical_key("53561_POI") == ("53561_POI", None)
    assert canonical_key("28558_PC") == ("28558_PC", None)
    loc = _loc_fields(
        {
            "Key": "3-53527_1_POI_AL",
            "Type": "POI",
            "IsAlias": True,
            "LocalizedName": "Bryant-Denny Stadium",
            "PrimaryPostalCode": "35401",
            "Country": {"ID": "US"},
            "AdministrativeArea": {"ID": "AL"},
            "ParentCity": {"LocalizedName": "Tuscaloosa"},
            "GeoPosition": {"Latitude": 33.208, "Longitude": -87.551, "Elevation": {"Imperial": {"Value": 199.0}}},
            "TimeZone": {"Name": "America/Chicago", "GmtOffset": -5.0},
            "Details": {"LocationStem": "us/saban-field-at-bryant-denny-stadium-al/35401"},
        }
    )
    assert loc["location_key"] == "53527_POI" and loc["alias_key"] == "3-53527_1_POI_AL" and loc["is_alias"] is True
    assert loc["parent_city"] == "Tuscaloosa" and loc["primary_postal_code"] == "35401" and loc["elevation_ft"] == 199.0
    assert loc["web_hourly_url"] == (
        "https://www.accuweather.com/en/us/saban-field-at-bryant-denny-stadium-al/35401/hourly-weather-forecast/53527_poi"
    )


def test_pick_location_prefers_city_then_canonical():
    venue = {"venue_state": "KY", "venue_country": "USA", "venue_city": "Lexington"}
    cands = [
        {
            "Key": "1_POI",
            "LocalizedName": "Kroger Field",
            "Country": {"ID": "US"},
            "AdministrativeArea": {"ID": "KY"},
            "ParentCity": {"LocalizedName": "Louisville"},
        },
        {
            "Key": "2-53578_1_POI_AL",
            "IsAlias": True,
            "LocalizedName": "Kroger Field",
            "Country": {"ID": "US"},
            "AdministrativeArea": {"ID": "KY"},
            "ParentCity": {"LocalizedName": "Lexington-Fayette"},
        },
        {
            "Key": "53578_POI",
            "LocalizedName": "Kroger Field",
            "Country": {"ID": "US"},
            "AdministrativeArea": {"ID": "KY"},
            "ParentCity": {"LocalizedName": "Lexington-Fayette"},
        },
    ]
    chosen, notes = pick_location(cands, venue, prefer_name="Kroger Field")
    assert chosen["Key"] == "53578_POI" and notes == []


def test_pick_location_filters_state_and_prefers_exact_name():
    venue = {"venue_state": "GA", "venue_country": "USA"}
    cands = [
        {
            "Key": "1_POI",
            "LocalizedName": "Sanford Stadium",
            "Country": {"ID": "US"},
            "AdministrativeArea": {"ID": "FL"},
        },
        {
            "Key": "2_POI",
            "LocalizedName": "Sanford Stadium Parking",
            "Country": {"ID": "US"},
            "AdministrativeArea": {"ID": "GA"},
        },
        {
            "Key": "53561_POI",
            "LocalizedName": "Sanford Stadium",
            "Country": {"ID": "US"},
            "AdministrativeArea": {"ID": "GA"},
        },
    ]
    chosen, notes = pick_location(cands, venue, prefer_name="Sanford Stadium")
    assert chosen["Key"] == "53561_POI" and notes == []
    chosen, notes = pick_location(cands[:1], venue, prefer_name="Sanford Stadium")
    assert chosen is None and "none in GA" in notes[0]
    ireland = {"venue_state": None, "venue_country": "Ireland"}
    chosen, _ = pick_location([{"Key": "207931", "LocalizedName": "Dublin", "Country": {"ID": "IE"}}], ireland)
    assert chosen["Key"] == "207931"


def test_venue_registry_merge_keeps_resolved_fields():
    venues = venues_from_games([GAME, {**GAME, "season_year": 2025}, DOME_GAME])
    assert (
        venues["3917"]["game_count"] == 2
        and venues["3917"]["first_season"] == 2025
        and venues["1964"]["venue_indoor"] is True
    )
    registry = {
        "meta": {},
        "venues": [{"venue_id": "3917", "location_key": "53561_POI", "method": "manual", "notes": []}],
    }
    merge_venues(registry, venues)
    idx = {v["venue_id"]: v for v in registry["venues"]}
    assert idx["3917"]["location_key"] == "53561_POI" and idx["3917"]["venue_name"] == "Sanford Stadium"
    assert (
        idx["1964"]["is_indoor"] is True and idx["1964"]["roof_type"] == "fixed" and idx["1964"]["location_key"] is None
    )


def test_games_in_scope_forecast_and_observed():
    now = datetime(2026, 9, 5, 18, 0, tzinfo=UTC)
    late = {**GAME, "game_id": "late", "kickoff_utc": "2026-09-06T23:30:00Z"}  # tomorrow: beyond a 12 h horizon
    played = {**GAME, "game_id": "played", "kickoff_utc": "2026-09-05T02:00:00Z"}
    tba = {**GAME, "game_id": "tba", "time_valid": False}
    scope = dict(
        (g["game_id"], st) for g, st in games_in_scope([GAME, late, played, tba], MODE_FORECAST, now, 12, PRE, DUR)
    )
    # `late` sits within a day past the horizon so it is recorded as outside_horizon; `played` is over and dropped
    assert scope == {"401756900": None, "late": "outside_horizon", "tba": "no_kickoff"}
    scope = dict(
        (g["game_id"], st)
        for g, st in games_in_scope([GAME, late, played], MODE_FORECAST, now, 12, PRE, DUR, game_ids=["late", "played"])
    )
    assert scope == {"late": "outside_horizon", "played": "outside_horizon"}
    scope = dict(
        (g["game_id"], st)
        for g, st in games_in_scope([GAME, late], MODE_FORECAST, now, 12, PRE, DUR, dates=["20260905"])
    )
    assert scope == {"401756900": None}
    morning_after = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    scope = dict(
        (g["game_id"], st) for g, st in games_in_scope([GAME, late, played], MODE_OBSERVED, morning_after, 24, PRE, DUR)
    )
    assert scope == {"401756900": None}  # `played` ended > 24 h ago, `late` not yet played


def _payload(rows, windows, snapshot=SNAP):
    return {
        "meta": {"snapshot_utc": snapshot, "pre_kickoff_hours": PRE, "game_duration_hours": DUR, "snapshot": "t"},
        "hourly": rows,
        "game_window": windows,
    }


def _registry():
    return {
        "meta": {},
        "venues": [
            {**LOCATION, "venue_name": "Sanford Stadium", "venue_state": "GA"},
            {
                "venue_id": "1964",
                "location_key": None,
                "is_indoor": True,
                "venue_name": "JMA Wireless Dome",
                "venue_state": "NY",
            },
        ],
    }


def test_validate_weather_passes_on_clean_payload():
    rows = parse_game_hours(PAYLOAD, GAME, LOCATION, SNAP, PRE, DUR)
    windows = [
        summarize_game_window(GAME, LOCATION, rows, SNAP, PRE, DUR, WINDOW_STATUS_FORECAST),
        summarize_game_window(
            DOME_GAME, None, [], SNAP, PRE, DUR, WINDOW_STATUS_INDOOR, SOURCE_ACCUWEATHER_FORECAST, True, "fixed"
        ),
    ]
    report = validate_weather(_payload(rows, windows), [GAME, DOME_GAME], _registry())
    failed = [c["name"] for c in report["checks"] if not c["passed"]]
    assert report["meta"]["status"] == "PASS", failed
    assert report["meta"]["status_counts"] == {"forecast": 1, "indoor_skipped": 1}
    assert report["meta"]["hourly_rows"] == 5 and report["meta"]["kind"] == "weather"


def test_validate_weather_catches_join_and_detail_errors():
    rows = parse_game_hours(PAYLOAD, GAME, LOCATION, SNAP, PRE, DUR)
    windows = [summarize_game_window(GAME, LOCATION, rows, SNAP, PRE, DUR, WINDOW_STATUS_FORECAST)]
    # wrong team id on one hour, a duplicated hour, an out-of-window hour flagged in-window, an impossible value
    bad = [dict(r) for r in rows]
    bad[0]["away_team_id"] = "999"
    bad.append(dict(rows[1]))
    stray = dict(rows[2])
    stray["forecast_for_utc"] = "2026-09-06T06:00:00Z"
    bad.append(stray)
    bad[3]["temp_f"] = 250.0
    bad[4]["wind_gust_mph"] = 1.0
    report = validate_weather(_payload(bad, windows), [GAME], _registry())
    failed = {c["name"]: c for c in report["checks"] if not c["passed"]}
    assert report["meta"]["status"] == "FAIL"
    assert failed["wx_team_ids_match_game"]["failure_count"] == 1
    assert failed["wx_no_duplicate_hours"]["failure_count"] == 1
    assert failed["wx_hours_within_window"]["failure_count"] == 1
    assert failed["wx_values_in_range"]["failures"][0]["field"] == "temp_f"
    assert "wx_gust_ge_wind" in failed and failed["wx_gust_ge_wind"]["level"] == "warn"
    assert "wx_window_counts_match_rows" in failed  # the stray hour changes the in-window count


def test_validate_weather_flags_indoor_rows_unresolved_and_partial_coverage():
    rows = parse_game_hours(PAYLOAD, GAME, LOCATION, SNAP, PRE, DUR)
    dome_rows = parse_game_hours(PAYLOAD, DOME_GAME, {"location_key": "X"}, SNAP, PRE, DUR)
    windows = [
        summarize_game_window(
            DOME_GAME, {"location_key": "X"}, dome_rows, SNAP, PRE, DUR, WINDOW_STATUS_FORECAST, is_indoor=True
        ),
        summarize_game_window(GAME, LOCATION, rows[:3], SNAP, PRE, DUR, WINDOW_STATUS_FORECAST),
        summarize_game_window(
            {**GAME, "game_id": "g3", "venue_id": "4444", "venue_name": "Nowhere Field"},
            None,
            [],
            SNAP,
            PRE,
            DUR,
            "unresolved_location",
        ),
    ]
    report = validate_weather(
        _payload(rows[:3] + dome_rows, windows),
        [GAME, DOME_GAME, {**GAME, "game_id": "g3", "venue_id": "4444"}],
        _registry(),
    )
    failed = {c["name"]: c for c in report["checks"] if not c["passed"]}
    assert failed["wx_indoor_games_have_no_hours"]["failure_count"] == 1
    assert failed["wx_outdoor_games_have_location"]["failures"][0]["game_id"] == "g3"
    assert failed["wx_window_coverage_complete"]["failures"][0]["hours_present"] == 3
    assert report["meta"]["status"] == "FAIL"


def test_validate_weather_requires_window_bounds():
    with pytest.raises(ValueError):
        validate_weather({"meta": {}, "hourly": [], "game_window": []}, [], None)
