"""Parse an Overtime.ag College Football offering dump into canonical odds rows.

Input is the `league.Lines` array from Overtime's Angular model (captured with
`overtime/capture.js`, or replayed from the `GetSportOffering` endpoint). Two shapes are
accepted:  {"lines": [ {...}, ... ]}  or the compact  {"cols": [...], "rows": [[...], ...]}.

Field semantics (verified against the rendered UI on 2026-09-05):
    Team1ID / Team2ID           away / home team names (as displayed)
    Team1RotNum / Team2RotNum   rotation numbers
    Spread + FavoredTeamID      favourite's line (negative or 0) + favourite name
    SpreadAdj1 / SpreadAdj2     spread price for Team1 (away) / Team2 (home)
    TotalPoints, TtlPtsAdj1/2   total, over price / under price
    MoneyLine1 / MoneyLine2     away / home moneyline
    TeamNTotalPoints, TeamNTtlPtsAdj1/2   team total, over / under price
    PeriodNumber                0 = Game, 1 = 1st Half, 3 = 1st Quarter
    GameDateTimeString          local time in US/Eastern ("MM/DD/YYYY HH:MM")
    GameDateTime                "/Date(<epoch ms>)/" (UTC epoch)

Spread convention follows betting-data-normalizing: one row per (game, period) with
`spread_magnitude` (>= 0) + `favorite_side`, plus explicit home/away perspective lines.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from ncaaf_clv.common import iso_utc, to_float, to_int
from ncaaf_clv.config import SOURCE_OVERTIME

log = logging.getLogger(__name__)

PERIOD_KEYS: dict[int, str] = {0: "game", 1: "1h", 2: "2h", 3: "1q", 4: "2q", 5: "3q", 6: "4q"}
_EPOCH_RE = re.compile(r"/Date\((-?\d+)\)/")
OT_DATETIME_FMT = "%m/%d/%Y %H:%M"


def _lines(raw: dict[str, Any]) -> list[dict[str, Any]]:
    if "lines" in raw:
        return list(raw["lines"])
    cols = raw.get("cols") or []
    return [dict(zip(cols, row, strict=True)) for row in raw.get("rows") or []]


def parse_ot_datetime(line: dict[str, Any], tz_name: str) -> datetime | None:
    """Prefer the local string (explicit tz); fall back to the epoch."""
    text = line.get("GameDateTimeString")
    if text:
        try:
            return datetime.strptime(text, OT_DATETIME_FMT).replace(tzinfo=ZoneInfo(tz_name)).astimezone(UTC)
        except ValueError:
            log.warning("bad GameDateTimeString %r", text)
    m = _EPOCH_RE.match(str(line.get("GameDateTime") or ""))
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=UTC)
    return None


def _favorite_side(line: dict[str, Any]) -> str | None:
    fav = line.get("FavoredTeamID")
    if fav is None:
        return None
    if fav == line.get("Team2ID"):
        return "home"
    if fav == line.get("Team1ID"):
        return "away"
    return None


def normalize_line(line: dict[str, Any], snapshot_utc: str, captured_at: str | None, tz_name: str) -> dict[str, Any]:
    period = to_int(line.get("PeriodNumber"))
    kickoff = parse_ot_datetime(line, tz_name)
    fav_side = _favorite_side(line)
    spread = to_float(line.get("Spread"))
    magnitude = abs(spread) if spread is not None else None
    home_spread = away_spread = None
    if magnitude is not None:
        if fav_side == "home":
            home_spread, away_spread = -magnitude, magnitude
        elif fav_side == "away":
            home_spread, away_spread = magnitude, -magnitude
        else:  # pick'em / unknown favourite
            home_spread = away_spread = 0.0 if magnitude == 0 else None
    return {
        "source": SOURCE_OVERTIME,
        "snapshot_utc": snapshot_utc,
        "captured_at": captured_at,
        "ot_game_num": str(line.get("GameNum")),
        "ot_parent_game_num": str(line["ParentGameNum"]) if line.get("ParentGameNum") else None,
        "sport_sub_type_id": to_int(line.get("SportSubTypeId")),
        "period_number": period,
        "period_key": PERIOD_KEYS.get(period, f"p{period}") if period is not None else "unknown",
        "period_description": line.get("PeriodDescription"),
        "away_team_raw": line.get("Team1ID"),
        "home_team_raw": line.get("Team2ID"),
        "away_rot": to_int(line.get("Team1RotNum")),
        "home_rot": to_int(line.get("Team2RotNum")),
        "kickoff_local": line.get("GameDateTimeString"),
        "kickoff_tz": tz_name,
        "kickoff_utc": iso_utc(kickoff) if kickoff else None,
        "favorite_side": fav_side,
        "favorite_team_raw": line.get("FavoredTeamID"),
        "spread_magnitude": magnitude,
        "home_spread": home_spread,
        "away_spread": away_spread,
        "away_spread_price": to_int(line.get("SpreadAdj1")),
        "home_spread_price": to_int(line.get("SpreadAdj2")),
        "total": to_float(line.get("TotalPoints")),
        "over_price": to_int(line.get("TtlPtsAdj1")),
        "under_price": to_int(line.get("TtlPtsAdj2")),
        "away_ml": to_int(line.get("MoneyLine1")),
        "home_ml": to_int(line.get("MoneyLine2")),
        "away_team_total": to_float(line.get("Team1TotalPoints")),
        "away_tt_over_price": to_int(line.get("Team1TtlPtsAdj1")),
        "away_tt_under_price": to_int(line.get("Team1TtlPtsAdj2")),
        "home_team_total": to_float(line.get("Team2TotalPoints")),
        "home_tt_over_price": to_int(line.get("Team2TtlPtsAdj1")),
        "home_tt_under_price": to_int(line.get("Team2TtlPtsAdj2")),
        "line_seq": to_int(line.get("LineSeq")),
        "status": line.get("Status"),
        "spread_status": line.get("SpreadLineStatus"),
        "ml_status": line.get("MoneyLineStatus"),
        "total_status": line.get("TotalsLineStatus"),
        "team_total_status": line.get("TeamTotalsLineStatus"),
        "schedule_comment": line.get("Comments"),
        "wager_cutoff_utc": iso_utc(datetime.fromtimestamp(int(m.group(1)) / 1000, tz=UTC))
        if (m := _EPOCH_RE.match(str(line.get("PeriodWagerCutoff") or "")))
        else None,
    }


def parse_overtime_dump(raw: dict[str, Any], tz_name: str, snapshot_utc: str | None = None) -> dict[str, Any]:
    """Return {"meta": ..., "odds": [row per (game, period)], "games": [row per game]}."""
    captured_at = raw.get("captured_at")
    snapshot_utc = snapshot_utc or captured_at or iso_utc()
    rows = [normalize_line(line, snapshot_utc, captured_at, tz_name) for line in _lines(raw)]
    rows.sort(key=lambda r: (r["kickoff_utc"] or "", r["ot_game_num"], r["period_number"] or 0))

    games: dict[str, dict[str, Any]] = {}
    for r in rows:
        g = games.setdefault(
            r["ot_game_num"],
            {
                "source": SOURCE_OVERTIME,
                "snapshot_utc": snapshot_utc,
                "ot_game_num": r["ot_game_num"],
                "away_team_raw": r["away_team_raw"],
                "home_team_raw": r["home_team_raw"],
                "away_rot": r["away_rot"],
                "home_rot": r["home_rot"],
                "kickoff_local": r["kickoff_local"],
                "kickoff_utc": r["kickoff_utc"],
                "periods": [],
            },
        )
        g["periods"].append(r["period_key"])

    league = raw.get("league") or {}
    meta = {
        "source": SOURCE_OVERTIME,
        "snapshot_utc": snapshot_utc,
        "captured_at": captured_at,
        "league": league,
        "kickoff_tz": tz_name,
        "line_count": len(rows),
        "game_count": len(games),
        "periods": raw.get("periods"),
    }
    return {"meta": meta, "odds": rows, "games": list(games.values())}
