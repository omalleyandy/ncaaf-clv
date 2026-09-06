"""Parsers turning AccuWeather hourly payloads into flat rows keyed on the ESPN game.

Every row carries: source, snapshot_utc (when fetched), game_id, home_team_id, away_team_id, venue_id,
location_key, forecast_for_utc (the hour), hour_offset_from_kickoff, lead_hours and in_game_window.

Game window = [kickoff - pre_kickoff_hours, kickoff + game_duration_hours]; an hourly item (which stands for
the hour starting at DateTime) is "in window" when its [t, t+1h) interval overlaps the window. The same rule
gives `hours_expected`, so coverage can be judged even when the API horizon ends before the window does.

Field mapping (hourly forecast, details=true; imperial):
    Temperature/RealFeel/RealFeelShade/WetBulb/DewPoint -> *_f      Wind.Speed / WindGust.Speed -> mph
    Precipitation/Rain/Snow/Ice/Thunderstorm probability -> 0-100     TotalLiquid/Rain/Snow/Ice -> inches
    CloudCover -> %   Ceiling -> ft   Visibility -> mi   UVIndex   Evapotranspiration in   SolarIrradiance W/m2
Observed conditions (historical/24) map into the same columns (Imperial sub-objects), plus pressure_inhg and
precip_1hr_in which only observations carry. Air quality and AccuLumen shown on the web site are not in the API.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Any

from ncaaf_clv.common import iso_utc, parse_espn_datetime, to_float, to_int
from ncaaf_clv.config import SOURCE_ACCUWEATHER_FORECAST, SOURCE_ACCUWEATHER_OBSERVED

log = logging.getLogger(__name__)

HOUR = timedelta(hours=1)

WINDOW_STATUS_FORECAST = "forecast"
WINDOW_STATUS_OBSERVED = "observed"
WINDOW_STATUS_INDOOR = "indoor_skipped"
WINDOW_STATUS_UNRESOLVED = "unresolved_location"
WINDOW_STATUS_OUTSIDE = "outside_horizon"
WINDOW_STATUS_NO_KICKOFF = "no_kickoff"
WINDOW_STATUS_API_ERROR = "api_error"
COVERED_STATUSES = frozenset({WINDOW_STATUS_FORECAST, WINDOW_STATUS_OBSERVED})


# ------------------------------------------------------------------------------ time helpers


def parse_aw_datetime(value: str | None, epoch: int | float | None = None) -> datetime | None:
    """AccuWeather 'DateTime' is local time with offset ('2026-09-05T19:00:00-04:00'); fall back to epoch."""
    if epoch is not None:
        try:
            return datetime.fromtimestamp(float(epoch), tz=UTC)
        except (TypeError, ValueError, OSError):
            pass
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        log.warning("Unparseable AccuWeather datetime: %r", value)
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(UTC)


def game_window(kickoff: datetime, pre_hours: float, duration_hours: float) -> tuple[datetime, datetime]:
    return kickoff - timedelta(hours=pre_hours), kickoff + timedelta(hours=duration_hours)


def hour_in_window(hour_start: datetime, window: tuple[datetime, datetime]) -> bool:
    """The hourly item covers [t, t+1h); count it when that interval overlaps the window."""
    start, end = window
    return hour_start < end and hour_start + HOUR > start


def expected_hours(window: tuple[datetime, datetime]) -> list[datetime]:
    """Hour-start instants whose hour overlaps the window (floored to the hour)."""
    start, end = window
    t = start.replace(minute=0, second=0, microsecond=0)
    out = []
    while t < end:
        if hour_in_window(t, window):
            out.append(t)
        t += HOUR
    return out


def reachable_hours(expected: list[datetime], snapshot: datetime | None, source: str | None) -> list[datetime]:
    """Window hours the API could return at snapshot time.

    forecast: AccuWeather's hourly feed starts at the *next* full hour after the request (verified 2026-09-05:
              a 23:14Z call returned 00:00Z first), so the current, partially elapsed hour is never available.
    observed: historical/24 carries an observation for every hour that has begun.
    Horizon limits are deliberately NOT applied: falling short of these hours is the signal to run again."""
    if snapshot is None:
        return list(expected)
    if source == SOURCE_ACCUWEATHER_OBSERVED:
        return [h for h in expected if h < snapshot]
    first = snapshot.replace(minute=0, second=0, microsecond=0)
    if snapshot > first:
        first += HOUR
    return [h for h in expected if h >= first]


def _hours_between(later: datetime, earlier: datetime) -> float:
    return round((later - earlier).total_seconds() / 3600.0, 3)


# ------------------------------------------------------------------------------ value helpers


def _val(obj: Any, *path: str) -> Any:
    cur = obj
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _imperial(obj: dict[str, Any] | None, *path: str) -> float | None:
    """Forecast payloads (metric=false) nest `{Value, Unit}`; observations nest `{Imperial: {Value}, Metric: {..}}`."""
    node = _val(obj, *path)
    if not isinstance(node, dict):
        return to_float(node)
    if "Imperial" in node:
        return to_float(_val(node, "Imperial", "Value"))
    return to_float(node.get("Value"))


# ------------------------------------------------------------------------------ row builders


def _join_keys(game: dict[str, Any], location: dict[str, Any], snapshot_utc: str, source: str) -> dict[str, Any]:
    return {
        "source": source,
        "snapshot_utc": snapshot_utc,
        "game_id": game["game_id"],
        "home_team_id": game.get("home_team_id"),
        "away_team_id": game.get("away_team_id"),
        "venue_id": game.get("venue_id"),
        "venue_name": game.get("venue_name"),
        "location_key": location.get("location_key"),
        "kickoff_utc": game.get("kickoff_utc"),
    }


def _timing(hour_utc: datetime, kickoff: datetime, snapshot: datetime, window: tuple[datetime, datetime]) -> dict:
    return {
        "forecast_for_utc": iso_utc(hour_utc),
        "hour_offset_from_kickoff": _hours_between(hour_utc, kickoff),
        "lead_hours": _hours_between(hour_utc, snapshot),
        "in_game_window": hour_in_window(hour_utc, window),
    }


def hourly_forecast_row(item: dict[str, Any]) -> dict[str, Any]:
    """Weather columns from one `forecasts/v1/hourly` item (no join keys)."""
    wind = item.get("Wind") or {}
    return {
        "forecast_for_local": item.get("DateTime"),
        "epoch": to_int(item.get("EpochDateTime")),
        "is_daylight": item.get("IsDaylight"),
        "weather_icon": to_int(item.get("WeatherIcon")),
        "icon_phrase": item.get("IconPhrase"),
        "has_precipitation": item.get("HasPrecipitation"),
        "precipitation_type": item.get("PrecipitationType"),
        "precipitation_intensity": item.get("PrecipitationIntensity"),
        "temp_f": _imperial(item, "Temperature"),
        "real_feel_f": _imperial(item, "RealFeelTemperature"),
        "real_feel_phrase": _val(item, "RealFeelTemperature", "Phrase"),
        "real_feel_shade_f": _imperial(item, "RealFeelTemperatureShade"),
        "wet_bulb_f": _imperial(item, "WetBulbTemperature"),
        "wet_bulb_globe_f": _imperial(item, "WetBulbGlobeTemperature"),
        "dew_point_f": _imperial(item, "DewPoint"),
        "relative_humidity": to_int(item.get("RelativeHumidity")),
        "indoor_relative_humidity": to_int(item.get("IndoorRelativeHumidity")),
        "wind_speed_mph": _imperial(wind, "Speed"),
        "wind_direction_deg": to_int(_val(wind, "Direction", "Degrees")),
        "wind_direction_text": _val(wind, "Direction", "English") or _val(wind, "Direction", "Localized"),
        "wind_gust_mph": _imperial(item, "WindGust", "Speed"),
        "precip_probability": to_int(item.get("PrecipitationProbability")),
        "rain_probability": to_int(item.get("RainProbability")),
        "snow_probability": to_int(item.get("SnowProbability")),
        "ice_probability": to_int(item.get("IceProbability")),
        "thunderstorm_probability": to_int(item.get("ThunderstormProbability")),
        "total_liquid_in": _imperial(item, "TotalLiquid"),
        "rain_in": _imperial(item, "Rain"),
        "snow_in": _imperial(item, "Snow"),
        "ice_in": _imperial(item, "Ice"),
        "cloud_cover": to_int(item.get("CloudCover")),
        "ceiling_ft": _imperial(item, "Ceiling"),
        "visibility_mi": _imperial(item, "Visibility"),
        "uv_index": to_int(item.get("UVIndex")),
        "uv_index_text": item.get("UVIndexText"),
        "evapotranspiration_in": _imperial(item, "Evapotranspiration"),
        "solar_irradiance_wm2": _imperial(item, "SolarIrradiance"),
        "pressure_inhg": None,
        "precip_1hr_in": None,
        "link": item.get("Link"),
        "mobile_link": item.get("MobileLink"),
    }


def observed_row(item: dict[str, Any]) -> dict[str, Any]:
    """Weather columns from one `currentconditions ... /historical/24` item, mapped into the forecast columns."""
    wind = item.get("Wind") or {}
    summary = item.get("PrecipitationSummary") or {}
    return {
        "forecast_for_local": item.get("LocalObservationDateTime"),
        "epoch": to_int(item.get("EpochTime")),
        "is_daylight": item.get("IsDayTime"),
        "weather_icon": to_int(item.get("WeatherIcon")),
        "icon_phrase": item.get("WeatherText"),
        "has_precipitation": item.get("HasPrecipitation"),
        "precipitation_type": item.get("PrecipitationType"),
        "precipitation_intensity": None,
        "temp_f": _imperial(item, "Temperature"),
        "real_feel_f": _imperial(item, "RealFeelTemperature"),
        "real_feel_phrase": _val(item, "RealFeelTemperature", "Imperial", "Phrase"),
        "real_feel_shade_f": _imperial(item, "RealFeelTemperatureShade"),
        "wet_bulb_f": _imperial(item, "WetBulbTemperature"),
        "wet_bulb_globe_f": _imperial(item, "WetBulbGlobeTemperature"),
        "dew_point_f": _imperial(item, "DewPoint"),
        "relative_humidity": to_int(item.get("RelativeHumidity")),
        "indoor_relative_humidity": to_int(item.get("IndoorRelativeHumidity")),
        "wind_speed_mph": _imperial(wind, "Speed"),
        "wind_direction_deg": to_int(_val(wind, "Direction", "Degrees")),
        "wind_direction_text": _val(wind, "Direction", "English") or _val(wind, "Direction", "Localized"),
        "wind_gust_mph": _imperial(item, "WindGust", "Speed"),
        "precip_probability": None,
        "rain_probability": None,
        "snow_probability": None,
        "ice_probability": None,
        "thunderstorm_probability": None,
        "total_liquid_in": _imperial(summary, "PastHour") if summary else _imperial(item, "Precip1hr"),
        "rain_in": None,
        "snow_in": None,
        "ice_in": None,
        "cloud_cover": to_int(item.get("CloudCover")),
        "ceiling_ft": _imperial(item, "Ceiling"),
        "visibility_mi": _imperial(item, "Visibility"),
        "uv_index": to_int(item.get("UVIndex")),
        "uv_index_text": item.get("UVIndexText"),
        "evapotranspiration_in": None,
        "solar_irradiance_wm2": None,
        "pressure_inhg": _imperial(item, "Pressure"),
        "precip_1hr_in": _imperial(item, "Precip1hr"),
        "link": item.get("Link"),
        "mobile_link": item.get("MobileLink"),
    }


def parse_game_hours(
    items: list[dict[str, Any]],
    game: dict[str, Any],
    location: dict[str, Any],
    snapshot_utc: str,
    pre_hours: float,
    duration_hours: float,
    source: str = SOURCE_ACCUWEATHER_FORECAST,
    keep_all_hours: bool = False,
) -> list[dict[str, Any]]:
    """Hourly rows for one game from a location's payload (forecast or observed). Rows are sorted by hour;
    only in-window hours are kept unless `keep_all_hours`."""
    kickoff = parse_espn_datetime(game.get("kickoff_utc"))
    if kickoff is None:
        return []
    snapshot = parse_espn_datetime(snapshot_utc)
    assert snapshot is not None
    window = game_window(kickoff, pre_hours, duration_hours)
    keys = _join_keys(game, location, snapshot_utc, source)
    rows: list[dict[str, Any]] = []
    for item in items or []:
        if source == SOURCE_ACCUWEATHER_OBSERVED:
            hour = parse_aw_datetime(item.get("LocalObservationDateTime"), item.get("EpochTime"))
            weather = observed_row(item)
        else:
            hour = parse_aw_datetime(item.get("DateTime"), item.get("EpochDateTime"))
            weather = hourly_forecast_row(item)
        if hour is None:
            continue
        # Observations are stamped at the top of the hour they summarise (e.g. 19:53 local -> 19:00).
        hour = hour.replace(minute=0, second=0, microsecond=0)
        timing = _timing(hour, kickoff, snapshot, window)
        if not timing["in_game_window"] and not keep_all_hours:
            continue
        rows.append({**keys, **timing, **weather})
    rows.sort(key=lambda r: r["forecast_for_utc"])
    return rows


# ------------------------------------------------------------------------------ window summary


def _mean(values: list[float | None]) -> float | None:
    xs = [v for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))]
    return round(sum(xs) / len(xs), 2) if xs else None


def _agg(values: list[Any], fn) -> float | None:
    xs = [v for v in values if v is not None]
    return fn(xs) if xs else None


def summarize_game_window(
    game: dict[str, Any],
    location: dict[str, Any] | None,
    rows: list[dict[str, Any]],
    snapshot_utc: str,
    pre_hours: float,
    duration_hours: float,
    status: str,
    source: str | None = None,
    is_indoor: bool = False,
    roof_type: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """One row per (game, source, snapshot): coverage of the kickoff window plus feature-ready aggregates.
    Written for every in-scope game, including indoor / unresolved ones (status says why there are no hours)."""
    kickoff = parse_espn_datetime(game.get("kickoff_utc"))
    snapshot = parse_espn_datetime(snapshot_utc)
    window = game_window(kickoff, pre_hours, duration_hours) if kickoff else None
    exp = expected_hours(window) if window else []
    in_window = [r for r in rows if r.get("in_game_window")]
    present = {r["forecast_for_utc"] for r in in_window}
    reachable = reachable_hours(exp, snapshot, source or (rows[0]["source"] if rows else None))
    location = location or {}
    out: dict[str, Any] = {
        "source": source or (rows[0]["source"] if rows else None),
        "snapshot_utc": snapshot_utc,
        "game_id": game["game_id"],
        "home_team_id": game.get("home_team_id"),
        "away_team_id": game.get("away_team_id"),
        "venue_id": game.get("venue_id"),
        "venue_name": game.get("venue_name"),
        "location_key": location.get("location_key"),
        "is_indoor": is_indoor,
        "roof_type": roof_type,
        "status": status,
        "note": note,
        "kickoff_utc": game.get("kickoff_utc"),
        "window_start_utc": iso_utc(window[0]) if window else None,
        "window_end_utc": iso_utc(window[1]) if window else None,
        "lead_hours_at_kickoff": _hours_between(kickoff, snapshot) if kickoff and snapshot else None,
        "hours_expected": len(exp),
        "hours_reachable": len(reachable),
        "hours_present": len(present),
        "coverage_pct": round(100.0 * len(present) / len(exp), 1) if exp else None,
        "first_hour_utc": in_window[0]["forecast_for_utc"] if in_window else None,
        "last_hour_utc": in_window[-1]["forecast_for_utc"] if in_window else None,
    }
    col = lambda k: [r.get(k) for r in in_window]  # noqa: E731
    out.update(
        {
            "temp_f_mean": _mean(col("temp_f")),
            "temp_f_min": _agg(col("temp_f"), min),
            "temp_f_max": _agg(col("temp_f"), max),
            "temp_f_at_kickoff": next((r["temp_f"] for r in in_window if 0 <= r["hour_offset_from_kickoff"] < 1), None),
            "real_feel_f_mean": _mean(col("real_feel_f")),
            "dew_point_f_mean": _mean(col("dew_point_f")),
            "relative_humidity_mean": _mean(col("relative_humidity")),
            "wind_speed_mph_mean": _mean(col("wind_speed_mph")),
            "wind_speed_mph_max": _agg(col("wind_speed_mph"), max),
            "wind_gust_mph_max": _agg(col("wind_gust_mph"), max),
            "wind_direction_deg_at_kickoff": next(
                (r["wind_direction_deg"] for r in in_window if 0 <= r["hour_offset_from_kickoff"] < 1), None
            ),
            "precip_probability_max": _agg(col("precip_probability"), max),
            "precip_probability_mean": _mean(col("precip_probability")),
            "thunderstorm_probability_max": _agg(col("thunderstorm_probability"), max),
            "total_liquid_in_sum": _agg(col("total_liquid_in"), lambda xs: round(sum(xs), 3)),
            "rain_in_sum": _agg(col("rain_in"), lambda xs: round(sum(xs), 3)),
            "snow_in_sum": _agg(col("snow_in"), lambda xs: round(sum(xs), 3)),
            "hours_with_precipitation": sum(1 for r in in_window if r.get("has_precipitation")),
            "cloud_cover_mean": _mean(col("cloud_cover")),
            "visibility_mi_min": _agg(col("visibility_mi"), min),
            "pressure_inhg_mean": _mean(col("pressure_inhg")),
            "icon_phrases": sorted({r["icon_phrase"] for r in in_window if r.get("icon_phrase")}),
        }
    )
    return out
