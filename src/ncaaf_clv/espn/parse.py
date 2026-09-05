"""Parsers turning raw ESPN JSON into flat, SQLite-ready row lists.

Every row carries `snapshot_utc` plus the ids needed for joins:
game_id (ESPN event id), team_id (ESPN team id), player_id (ESPN athlete id).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ncaaf_clv.common import iso_utc, parse_espn_datetime, to_float, to_int
from ncaaf_clv.config import SOURCE_ESPN

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- helpers


def _team_row(team: dict[str, Any]) -> dict[str, Any]:
    """Normalise the many shapes ESPN uses for a team stub."""
    logos = team.get("logos") or []
    logo = team.get("logo") or (logos[0].get("href") if logos else None)
    return {
        "team_id": str(team.get("id")) if team.get("id") is not None else None,
        "uid": team.get("uid"),
        "slug": team.get("slug"),
        "location": team.get("location"),
        "nickname": team.get("name") or team.get("nickname"),
        "abbreviation": team.get("abbreviation"),
        "display_name": team.get("displayName"),
        "short_display_name": team.get("shortDisplayName"),
        "color": team.get("color"),
        "alternate_color": team.get("alternateColor"),
        "logo": logo,
    }


def _status_row(status: dict[str, Any] | None) -> dict[str, Any]:
    status = status or {}
    st = status.get("type") or {}
    return {
        "status_id": st.get("id"),
        "status_name": st.get("name"),
        "status_state": st.get("state"),
        "status_completed": st.get("completed"),
        "status_detail": st.get("detail"),
        "clock": status.get("clock"),
        "display_clock": status.get("displayClock"),
        "period": status.get("period"),
    }


def _venue_row(venue: dict[str, Any] | None) -> dict[str, Any]:
    venue = venue or {}
    addr = venue.get("address") or {}
    return {
        "venue_id": str(venue["id"]) if venue.get("id") is not None else None,
        "venue_name": venue.get("fullName"),
        "venue_city": addr.get("city"),
        "venue_state": addr.get("state"),
        "venue_country": addr.get("country"),
        "venue_indoor": venue.get("indoor"),
        "venue_grass": venue.get("grass"),
    }


def _records(competitor: dict[str, Any]) -> dict[str, str | None]:
    out: dict[str, str | None] = {"record_overall": None, "record_home": None, "record_away": None, "record_conf": None}
    for rec in competitor.get("records") or competitor.get("record") or []:
        rtype = (rec.get("type") or "").lower()
        summary = rec.get("summary") or rec.get("displayValue")
        if rtype == "total":
            out["record_overall"] = summary
        elif rtype in ("homerecord", "home"):
            out["record_home"] = summary
        elif rtype in ("awayrecord", "away", "road"):
            out["record_away"] = summary
        elif rtype == "vsconf":
            out["record_conf"] = summary
    return out


def _linescores(competitor: dict[str, Any]) -> list[float | None]:
    return [to_float(ls.get("value", ls.get("displayValue"))) for ls in competitor.get("linescores") or []]


def _odds_line_value(text: str | None) -> float | None:
    """'o50.5' / 'u44.5' / '-21' -> float."""
    if text is None:
        return None
    return to_float(str(text).lstrip("ou"))


def parse_espn_odds(odds: list[dict[str, Any]], game_id: str, snapshot_utc: str) -> list[dict[str, Any]]:
    """ESPN's embedded sportsbook odds (DraftKings via pickcenter). Spread is stored canonically:
    `spread_magnitude` (positive) + `favorite_team_id`, plus home/away perspective lines."""
    rows: list[dict[str, Any]] = []
    for o in odds or []:
        provider = o.get("provider") or {}
        home = o.get("homeTeamOdds") or {}
        away = o.get("awayTeamOdds") or {}
        home_tid = str(home.get("teamId") or (home.get("team") or {}).get("id") or "") or None
        away_tid = str(away.get("teamId") or (away.get("team") or {}).get("id") or "") or None
        spread_home = to_float(o.get("spread"))  # ESPN 'spread' is from the home team's perspective
        fav_tid = None
        if home.get("favorite"):
            fav_tid = home_tid
        elif away.get("favorite"):
            fav_tid = away_tid
        ps = o.get("pointSpread") or {}
        ml = o.get("moneyline") or {}
        tot = o.get("total") or {}

        def _leaf(d: dict[str, Any], *path: str) -> Any:
            cur: Any = d
            for p in path:
                cur = (cur or {}).get(p)
            return cur

        rows.append(
            {
                "source": SOURCE_ESPN,
                "snapshot_utc": snapshot_utc,
                "game_id": game_id,
                "provider_id": str(provider.get("id")) if provider.get("id") else None,
                "provider_name": provider.get("name"),
                "details": o.get("details"),
                "home_team_id": home_tid,
                "away_team_id": away_tid,
                "favorite_team_id": fav_tid,
                "spread_magnitude": abs(spread_home) if spread_home is not None else None,
                "home_spread": spread_home,
                "away_spread": -spread_home if spread_home is not None else None,
                "total": to_float(o.get("overUnder")),
                "home_ml": to_int(home.get("moneyLine")) or to_int(_leaf(ml, "home", "close", "odds")),
                "away_ml": to_int(away.get("moneyLine")) or to_int(_leaf(ml, "away", "close", "odds")),
                "home_spread_price": to_int(home.get("spreadOdds")) or to_int(_leaf(ps, "home", "close", "odds")),
                "away_spread_price": to_int(away.get("spreadOdds")) or to_int(_leaf(ps, "away", "close", "odds")),
                "over_price": to_int(o.get("overOdds")) or to_int(_leaf(tot, "over", "close", "odds")),
                "under_price": to_int(o.get("underOdds")) or to_int(_leaf(tot, "under", "close", "odds")),
                "open_home_spread": _odds_line_value(_leaf(ps, "home", "open", "line")),
                "open_total": _odds_line_value(_leaf(tot, "over", "open", "line")),
                "open_home_ml": to_int(_leaf(ml, "home", "open", "odds")),
                "open_away_ml": to_int(_leaf(ml, "away", "open", "odds")),
                "close_home_spread": _odds_line_value(_leaf(ps, "home", "close", "line")),
                "close_total": _odds_line_value(_leaf(tot, "over", "close", "line")),
            }
        )
    return rows


def _american(block: dict[str, Any] | None, *path: str) -> str | None:
    """Walk `block[path...]`; core-API price/line objects expose the value as `american`."""
    cur: Any = block
    for key in path:
        cur = (cur or {}).get(key)
    if isinstance(cur, dict):
        return cur.get("american") or cur.get("alternateDisplayValue")
    return cur


def _core_line(text: str | None) -> float | None:
    """'-2.5' / '+7' / 'EVEN' / 'PK' -> spread or total line."""
    if text is None:
        return None
    if str(text).strip().upper() in {"EVEN", "PK", "PICK"}:
        return 0.0
    return to_float(text)


def _core_price(text: str | None) -> int | None:
    """'-110' / '+105' / 'EVEN' -> American price."""
    if text is None:
        return None
    if str(text).strip().upper() == "EVEN":
        return 100
    return to_int(text)


def parse_core_odds(
    items: list[dict[str, Any]],
    game_id: str,
    home_team_id: str | None,
    away_team_id: str | None,
    snapshot_utc: str,
) -> list[dict[str, Any]]:
    """Core API `events/<id>/competitions/<id>/odds` items -> espn_odds rows, one per sportsbook.

    Top-level `spread` (home perspective) / `overUnder` and the `current` blocks are the last line
    ESPN recorded, i.e. the de-facto close for a finished game. `open_*` / `close_*` are filled only
    from explicit open/close blocks (ESPN BET from 2023, most books from 2024); rows without them
    keep those columns NULL rather than relabelling `current` as a close."""
    rows: list[dict[str, Any]] = []
    for o in items or []:
        provider = o.get("provider") or {}
        home = o.get("homeTeamOdds") or {}
        away = o.get("awayTeamOdds") or {}
        spread_home = to_float(o.get("spread"))
        fav_tid = home_team_id if home.get("favorite") else (away_team_id if away.get("favorite") else None)
        rows.append(
            {
                "source": SOURCE_ESPN,
                "snapshot_utc": snapshot_utc,
                "game_id": game_id,
                "provider_id": str(provider.get("id")) if provider.get("id") else None,
                "provider_name": provider.get("name"),
                "details": o.get("details"),
                "home_team_id": home_team_id,
                "away_team_id": away_team_id,
                "favorite_team_id": fav_tid,
                "spread_magnitude": abs(spread_home) if spread_home is not None else None,
                "home_spread": spread_home,
                "away_spread": -spread_home if spread_home is not None else None,
                "total": to_float(o.get("overUnder")),
                "home_ml": to_int(home.get("moneyLine")) or _core_price(_american(home, "current", "moneyLine")),
                "away_ml": to_int(away.get("moneyLine")) or _core_price(_american(away, "current", "moneyLine")),
                "home_spread_price": to_int(home.get("spreadOdds"))
                or _core_price(_american(home, "current", "spread")),
                "away_spread_price": to_int(away.get("spreadOdds"))
                or _core_price(_american(away, "current", "spread")),
                "over_price": to_int(o.get("overOdds")) or _core_price(_american(o, "current", "over")),
                "under_price": to_int(o.get("underOdds")) or _core_price(_american(o, "current", "under")),
                "open_home_spread": _core_line(_american(home, "open", "pointSpread")),
                "open_total": _core_line(_american(o, "open", "total")),
                "open_home_ml": _core_price(_american(home, "open", "moneyLine")),
                "open_away_ml": _core_price(_american(away, "open", "moneyLine")),
                "close_home_spread": _core_line(_american(home, "close", "pointSpread")),
                "close_total": _core_line(_american(o, "close", "total")),
            }
        )
    return rows


# ------------------------------------------------------------------------ scoreboard


def parse_scoreboard(payload: dict[str, Any], snapshot_utc: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """Scoreboard -> games, game_teams (one per side), espn_odds."""
    snapshot_utc = snapshot_utc or iso_utc()
    games: list[dict[str, Any]] = []
    game_teams: list[dict[str, Any]] = []
    odds_rows: list[dict[str, Any]] = []
    for ev in payload.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        game_id = str(ev["id"])
        competitors = comp.get("competitors") or []
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        kickoff = parse_espn_datetime(ev.get("date"))
        broadcasts = [b.get("names", [None])[0] for b in comp.get("broadcasts") or [] if b.get("names")]
        game = {
            "source": SOURCE_ESPN,
            "snapshot_utc": snapshot_utc,
            "game_id": game_id,
            "uid": ev.get("uid"),
            "name": ev.get("name"),
            "short_name": ev.get("shortName"),
            "kickoff_utc": iso_utc(kickoff) if kickoff else None,
            "time_valid": comp.get("timeValid", ev.get("timeValid")),
            "season_year": (ev.get("season") or {}).get("year"),
            "season_type": (ev.get("season") or {}).get("type"),
            "week": (ev.get("week") or {}).get("number"),
            "neutral_site": comp.get("neutralSite"),
            "conference_game": comp.get("conferenceCompetition"),
            "attendance": to_int(comp.get("attendance")),
            "home_team_id": str(home["team"]["id"]) if home else None,
            "away_team_id": str(away["team"]["id"]) if away else None,
            "home_score": to_int((home or {}).get("score")),
            "away_score": to_int((away or {}).get("score")),
            "broadcast": ",".join(b for b in broadcasts if b) or None,
            "notes": "; ".join(n.get("headline", "") for n in comp.get("notes") or []) or None,
            **_status_row(comp.get("status") or ev.get("status")),
            **_venue_row(comp.get("venue")),
        }
        games.append(game)
        for side in (home, away):
            if not side:
                continue
            game_teams.append(
                {
                    "source": SOURCE_ESPN,
                    "snapshot_utc": snapshot_utc,
                    "game_id": game_id,
                    "team_id": str(side["team"]["id"]),
                    "home_away": side.get("homeAway"),
                    "score": to_int(side.get("score")),
                    "winner": side.get("winner"),
                    "curated_rank": (side.get("curatedRank") or {}).get("current"),
                    "linescores": _linescores(side),
                    **_records(side),
                    **{f"team_{k}": v for k, v in _team_row(side["team"]).items() if k != "team_id"},
                }
            )
        odds_rows.extend(parse_espn_odds(comp.get("odds") or [], game_id, snapshot_utc))
    return {"games": games, "game_teams": game_teams, "espn_odds": odds_rows}


# --------------------------------------------------------------------------- summary


def parse_gamecast(payload: dict[str, Any], snapshot_utc: str | None = None) -> dict[str, Any]:
    """Gamecast tab: header/game info, competitors, linescores, scoring plays, leaders,
    win probability, ESPN odds (open/close)."""
    snapshot_utc = snapshot_utc or iso_utc()
    header = payload.get("header") or {}
    comp = (header.get("competitions") or [{}])[0]
    game_id = str(header.get("id") or comp.get("id"))
    kickoff = parse_espn_datetime(comp.get("date"))
    game_info = payload.get("gameInfo") or {}
    competitors = comp.get("competitors") or []
    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away = next((c for c in competitors if c.get("homeAway") == "away"), None)

    game = {
        "source": SOURCE_ESPN,
        "snapshot_utc": snapshot_utc,
        "game_id": game_id,
        "uid": header.get("uid"),
        "kickoff_utc": iso_utc(kickoff) if kickoff else None,
        "season_year": (header.get("season") or {}).get("year"),
        "season_type": (header.get("season") or {}).get("type"),
        "week": header.get("week"),
        "neutral_site": comp.get("neutralSite"),
        "conference_game": comp.get("conferenceCompetition"),
        "boxscore_available": comp.get("boxscoreAvailable"),
        "boxscore_source": comp.get("boxscoreSource"),
        "play_by_play_source": comp.get("playByPlaySource"),
        "attendance": to_int(game_info.get("attendance")),
        "home_team_id": str(home["team"]["id"]) if home else None,
        "away_team_id": str(away["team"]["id"]) if away else None,
        "home_score": to_int((home or {}).get("score")),
        "away_score": to_int((away or {}).get("score")),
        **_status_row(comp.get("status")),
        **_venue_row(game_info.get("venue")),
    }

    teams = []
    for side in (home, away):
        if not side:
            continue
        teams.append(
            {
                "source": SOURCE_ESPN,
                "snapshot_utc": snapshot_utc,
                "game_id": game_id,
                "team_id": str(side["team"]["id"]),
                "home_away": side.get("homeAway"),
                "score": to_int(side.get("score")),
                "winner": side.get("winner"),
                "rank": side.get("rank"),
                "linescores": _linescores(side),
                **_records(side),
                **{f"team_{k}": v for k, v in _team_row(side["team"]).items() if k != "team_id"},
            }
        )

    scoring = []
    for sp in payload.get("scoringPlays") or []:
        scoring.append(
            {
                "source": SOURCE_ESPN,
                "snapshot_utc": snapshot_utc,
                "game_id": game_id,
                "play_id": str(sp.get("id")),
                "team_id": str((sp.get("team") or {}).get("id")) if sp.get("team") else None,
                "period": (sp.get("period") or {}).get("number"),
                "clock": (sp.get("clock") or {}).get("displayValue"),
                "clock_seconds": to_float((sp.get("clock") or {}).get("value")),
                "play_type": (sp.get("type") or {}).get("text"),
                "scoring_type": (sp.get("scoringType") or {}).get("name"),
                "text": sp.get("text"),
                "away_score": to_int(sp.get("awayScore")),
                "home_score": to_int(sp.get("homeScore")),
            }
        )

    leaders = []
    for team_block in payload.get("leaders") or []:
        tid = str((team_block.get("team") or {}).get("id"))
        for cat in team_block.get("leaders") or []:
            for rank, leader in enumerate(cat.get("leaders") or [], start=1):
                ath = leader.get("athlete") or {}
                leaders.append(
                    {
                        "source": SOURCE_ESPN,
                        "snapshot_utc": snapshot_utc,
                        "game_id": game_id,
                        "team_id": tid,
                        "category": cat.get("name"),
                        "rank": rank,
                        "player_id": str(ath.get("id")) if ath.get("id") else None,
                        "player_name": ath.get("displayName"),
                        "position": (ath.get("position") or {}).get("abbreviation"),
                        "display_value": leader.get("displayValue"),
                        "value": to_float(leader.get("value")),
                    }
                )

    win_prob = [
        {
            "source": SOURCE_ESPN,
            "snapshot_utc": snapshot_utc,
            "game_id": game_id,
            "play_id": str(wp.get("playId")) if wp.get("playId") else None,
            "seq": i,
            "home_win_pct": to_float(wp.get("homeWinPercentage")),
            "tie_pct": to_float(wp.get("tiePercentage")),
        }
        for i, wp in enumerate(payload.get("winprobability") or [])
    ]

    odds_rows = parse_espn_odds(payload.get("pickcenter") or [], game_id, snapshot_utc)
    return {
        "game": game,
        "teams": teams,
        "scoring_plays": scoring,
        "leaders": leaders,
        "win_probability": win_prob,
        "espn_odds": odds_rows,
    }


def parse_boxscore(payload: dict[str, Any], snapshot_utc: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """Box Score tab: team_stats (EAV) and player_stats (EAV, one row per stat)."""
    snapshot_utc = snapshot_utc or iso_utc()
    header = payload.get("header") or {}
    game_id = str(header.get("id") or ((header.get("competitions") or [{}])[0]).get("id"))
    box = payload.get("boxscore") or {}

    team_stats = []
    for tb in box.get("teams") or []:
        tid = str((tb.get("team") or {}).get("id"))
        for st in tb.get("statistics") or []:
            team_stats.append(
                {
                    "source": SOURCE_ESPN,
                    "snapshot_utc": snapshot_utc,
                    "game_id": game_id,
                    "team_id": tid,
                    "home_away": tb.get("homeAway"),
                    "stat_name": st.get("name"),
                    "stat_label": st.get("label"),
                    "display_value": st.get("displayValue"),
                    "value": to_float(st.get("value")),
                }
            )

    player_stats = []
    for pb in box.get("players") or []:
        tid = str((pb.get("team") or {}).get("id"))
        for cat in pb.get("statistics") or []:
            keys = cat.get("keys") or []
            labels = cat.get("labels") or []
            for entry in cat.get("athletes") or []:
                ath = entry.get("athlete") or {}
                stats = entry.get("stats") or []
                for idx, raw in enumerate(stats):
                    key = keys[idx] if idx < len(keys) else f"stat_{idx}"
                    player_stats.append(
                        {
                            "source": SOURCE_ESPN,
                            "snapshot_utc": snapshot_utc,
                            "game_id": game_id,
                            "team_id": tid,
                            "player_id": str(ath.get("id")) if ath.get("id") else None,
                            "player_name": ath.get("displayName"),
                            "jersey": ath.get("jersey"),
                            "category": cat.get("name"),
                            "stat_key": key,
                            "stat_label": labels[idx] if idx < len(labels) else None,
                            "display_value": raw,
                            "value": to_float(raw),
                        }
                    )
    return {"team_stats": team_stats, "player_stats": player_stats}


def parse_play_by_play(payload: dict[str, Any], snapshot_utc: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """Play-By-Play tab: drives and plays (from `drives.previous` + `drives.current`)."""
    snapshot_utc = snapshot_utc or iso_utc()
    header = payload.get("header") or {}
    game_id = str(header.get("id") or ((header.get("competitions") or [{}])[0]).get("id"))
    drives_payload = payload.get("drives") or {}
    drive_list = list(drives_payload.get("previous") or [])
    current = drives_payload.get("current")
    if current and current.get("id") not in {d.get("id") for d in drive_list}:
        drive_list.append(current)

    drives: list[dict[str, Any]] = []
    plays: list[dict[str, Any]] = []
    for order, dr in enumerate(drive_list, start=1):
        drive_id = str(dr.get("id"))
        tid = str((dr.get("team") or {}).get("id")) if dr.get("team") else None
        start, end = dr.get("start") or {}, dr.get("end") or {}
        drives.append(
            {
                "source": SOURCE_ESPN,
                "snapshot_utc": snapshot_utc,
                "game_id": game_id,
                "drive_id": drive_id,
                "drive_order": order,
                "team_id": tid,
                "description": dr.get("description"),
                "result": dr.get("result"),
                "display_result": dr.get("displayResult"),
                "is_score": dr.get("isScore"),
                "yards": to_int(dr.get("yards")),
                "offensive_plays": to_int(dr.get("offensivePlays")),
                "time_elapsed": (dr.get("timeElapsed") or {}).get("displayValue"),
                "start_period": (start.get("period") or {}).get("number"),
                "start_clock": (start.get("clock") or {}).get("displayValue"),
                "start_yard_line": to_int(start.get("yardLine")),
                "start_text": start.get("text"),
                "end_period": (end.get("period") or {}).get("number"),
                "end_clock": (end.get("clock") or {}).get("displayValue"),
                "end_yard_line": to_int(end.get("yardLine")),
                "end_text": end.get("text"),
            }
        )
        for pl in dr.get("plays") or []:
            ps, pe = pl.get("start") or {}, pl.get("end") or {}
            offense = next((tp for tp in pl.get("teamParticipants") or [] if tp.get("type") == "offense"), {})
            plays.append(
                {
                    "source": SOURCE_ESPN,
                    "snapshot_utc": snapshot_utc,
                    "game_id": game_id,
                    "drive_id": drive_id,
                    "play_id": str(pl.get("id")),
                    "sequence_number": to_int(pl.get("sequenceNumber")),
                    "period": (pl.get("period") or {}).get("number"),
                    "clock": (pl.get("clock") or {}).get("displayValue"),
                    "offense_team_id": str(offense.get("id")) if offense.get("id") else tid,
                    "play_type_id": (pl.get("type") or {}).get("id"),
                    "play_type": (pl.get("type") or {}).get("text"),
                    "text": pl.get("text"),
                    "away_score": to_int(pl.get("awayScore")),
                    "home_score": to_int(pl.get("homeScore")),
                    "scoring_play": pl.get("scoringPlay"),
                    "is_penalty": pl.get("isPenalty"),
                    "is_turnover": pl.get("isTurnover"),
                    "stat_yardage": to_int(pl.get("statYardage")),
                    "start_down": to_int(ps.get("down")),
                    "start_distance": to_int(ps.get("distance")),
                    "start_yard_line": to_int(ps.get("yardLine")),
                    "start_yards_to_endzone": to_int(ps.get("yardsToEndzone")),
                    "start_possession_team_id": str((ps.get("team") or {}).get("id")) if ps.get("team") else None,
                    "end_down": to_int(pe.get("down")),
                    "end_distance": to_int(pe.get("distance")),
                    "end_yard_line": to_int(pe.get("yardLine")),
                    "end_yards_to_endzone": to_int(pe.get("yardsToEndzone")),
                    "end_possession_team_id": str((pe.get("team") or {}).get("id")) if pe.get("team") else None,
                    "down_distance_text": pe.get("downDistanceText"),
                    "wallclock_utc": pl.get("wallclock"),
                }
            )
    return {"drives": drives, "plays": plays}


# ----------------------------------------------------------------------------- teams


def parse_team_detail(payload: dict[str, Any], snapshot_utc: str | None = None) -> dict[str, Any]:
    snapshot_utc = snapshot_utc or iso_utc()
    team = payload.get("team") or payload
    groups = team.get("groups") or {}
    row = _team_row(team)
    row.update(
        {
            "source": SOURCE_ESPN,
            "snapshot_utc": snapshot_utc,
            "is_active": team.get("isActive"),
            "conference_group_id": str(groups.get("id")) if groups.get("id") else None,
            "parent_group_id": str((groups.get("parent") or {}).get("id")) if groups.get("parent") else None,
            "is_conference": groups.get("isConference"),
            "standing_summary": team.get("standingSummary"),
        }
    )
    return row


def parse_core_season_team(payload: dict[str, Any], snapshot_utc: str | None = None) -> dict[str, Any]:
    """Core API `seasons/<year>/teams/<id>` -> team dimension row for that season.

    `group_id` is the team's group *in that season* (a conference, or a division whose parent is
    the conference); the collector resolves it to conference/division ids. `is_all_star` marks the
    exhibition squads ESPN files under group 80."""
    snapshot_utc = snapshot_utc or iso_utc()
    row = _team_row(payload)
    group_ref = (payload.get("groups") or {}).get("$ref") or ""
    m = re.search(r"/groups/(\d+)", group_ref)
    row.update(
        {
            "source": SOURCE_ESPN,
            "snapshot_utc": snapshot_utc,
            "is_active": payload.get("isActive"),
            "is_all_star": bool(payload.get("isAllStar")),
            "group_id": m.group(1) if m else None,
            "standing_summary": None,
        }
    )
    return row


def parse_team_statistics(payload: dict[str, Any], snapshot_utc: str | None = None) -> list[dict[str, Any]]:
    """Team season statistics (EAV). `side` = 'team' or 'opponent'."""
    snapshot_utc = snapshot_utc or iso_utc()
    team_id = str((payload.get("team") or {}).get("id"))
    season = payload.get("season") or {}
    results = payload.get("results") or {}
    rows: list[dict[str, Any]] = []

    def _walk(block: dict[str, Any] | None, side: str) -> None:
        for cat in (block or {}).get("categories") or []:
            for st in cat.get("stats") or []:
                rows.append(
                    {
                        "source": SOURCE_ESPN,
                        "snapshot_utc": snapshot_utc,
                        "team_id": team_id,
                        "season_year": season.get("year"),
                        "season_type": season.get("type"),
                        "side": side,
                        "category": cat.get("name"),
                        "stat_name": st.get("name"),
                        "stat_abbr": st.get("abbreviation"),
                        "display_name": st.get("displayName"),
                        "value": to_float(st.get("value")),
                        "display_value": st.get("displayValue"),
                        "per_game_value": to_float(st.get("perGameValue")),
                        "rank": to_int(st.get("rank")),
                    }
                )

    _walk(results.get("stats"), "team")
    opp = results.get("opponent")
    if isinstance(opp, dict):  # same shape as `stats`
        _walk(opp, "opponent")
    elif isinstance(opp, list):  # bare list of categories
        _walk({"categories": opp}, "opponent")
    return rows


def parse_team_schedule(payload: dict[str, Any], snapshot_utc: str | None = None) -> list[dict[str, Any]]:
    """One row per (team, game) from the team's schedule page."""
    snapshot_utc = snapshot_utc or iso_utc()
    team_id = str((payload.get("team") or {}).get("id"))
    rows: list[dict[str, Any]] = []
    for ev in payload.get("events") or []:
        comp = (ev.get("competitions") or [{}])[0]
        competitors = comp.get("competitors") or []
        me = next((c for c in competitors if str((c.get("team") or {}).get("id")) == team_id), None)
        opp = next((c for c in competitors if str((c.get("team") or {}).get("id")) != team_id), None)
        kickoff = parse_espn_datetime(ev.get("date"))
        rows.append(
            {
                "source": SOURCE_ESPN,
                "snapshot_utc": snapshot_utc,
                "team_id": team_id,
                "game_id": str(ev.get("id")),
                "name": ev.get("name"),
                "short_name": ev.get("shortName"),
                "kickoff_utc": iso_utc(kickoff) if kickoff else None,
                "time_valid": ev.get("timeValid"),
                "season_year": (ev.get("season") or {}).get("year"),
                "season_type": (ev.get("seasonType") or {}).get("type") or (ev.get("season") or {}).get("type"),
                "week": (ev.get("week") or {}).get("number"),
                "home_away": (me or {}).get("homeAway"),
                "neutral_site": comp.get("neutralSite"),
                "opponent_team_id": str((opp or {}).get("team", {}).get("id")) if opp else None,
                "opponent_display_name": ((opp or {}).get("team") or {}).get("displayName"),
                "team_score": to_int((me or {}).get("score")),
                "opponent_score": to_int((opp or {}).get("score")),
                "winner": (me or {}).get("winner"),
                "curated_rank": ((me or {}).get("curatedRank") or {}).get("current"),
                **_status_row(comp.get("status")),
                **_venue_row(comp.get("venue")),
            }
        )
    return rows


def parse_team_roster(payload: dict[str, Any], snapshot_utc: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """Roster -> players (one per athlete) and coaches."""
    snapshot_utc = snapshot_utc or iso_utc()
    team_id = str((payload.get("team") or {}).get("id"))
    season = payload.get("season") or {}
    players: list[dict[str, Any]] = []
    for group in payload.get("athletes") or []:
        for ath in group.get("items") or []:
            pos = ath.get("position") or {}
            exp = ath.get("experience") or {}
            birth = ath.get("birthPlace") or {}
            status = ath.get("status") or {}
            injuries = ath.get("injuries") or []
            players.append(
                {
                    "source": SOURCE_ESPN,
                    "snapshot_utc": snapshot_utc,
                    "team_id": team_id,
                    "season_year": season.get("year"),
                    "player_id": str(ath.get("id")),
                    "uid": ath.get("uid"),
                    "first_name": ath.get("firstName"),
                    "last_name": ath.get("lastName"),
                    "display_name": ath.get("displayName"),
                    "jersey": ath.get("jersey"),
                    "roster_group": group.get("position"),
                    "position": pos.get("abbreviation"),
                    "position_name": pos.get("name"),
                    "position_group": (pos.get("parent") or {}).get("abbreviation"),
                    "height_in": to_float(ath.get("height")),
                    "weight_lb": to_float(ath.get("weight")),
                    "experience_years": to_int(exp.get("years")),
                    "class_year": exp.get("displayValue"),
                    "birth_city": birth.get("city"),
                    "birth_state": birth.get("state"),
                    "birth_country": birth.get("country"),
                    "status": status.get("name"),
                    "injury_status": (injuries[0].get("status") if injuries else None),
                    "injury_detail": ((injuries[0].get("details") or {}).get("type") if injuries else None),
                }
            )
    coaches = [
        {
            "source": SOURCE_ESPN,
            "snapshot_utc": snapshot_utc,
            "team_id": team_id,
            "season_year": season.get("year"),
            "coach_id": str(c.get("id")),
            "first_name": c.get("firstName"),
            "last_name": c.get("lastName"),
            "experience_years": to_int(c.get("experience")),
        }
        for c in payload.get("coach") or []
    ]
    return {"players": players, "coaches": coaches}
