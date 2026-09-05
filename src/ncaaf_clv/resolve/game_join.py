"""Join Overtime.ag games (GameNum, team names, ET kickoff) to ESPN games (game_id, team ids, UTC).

Strategy (deterministic):
    1. resolve both team names to ESPN team ids (TeamResolver)
    2. candidate ESPN games = same unordered team pair
    3. prefer same orientation (away/home); allow swapped orientation for neutral-site games
    4. pick the candidate whose kickoff is nearest; reject if outside `window_hours`
Kickoff times differ between sources (delays, TBA slots, tz conversions), so the team pair is
the primary key and time is only a tie-breaker / sanity bound.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ncaaf_clv.common import iso_utc, parse_espn_datetime, utc_now
from ncaaf_clv.resolve.team_resolver import ResolvedTeam, TeamResolver

log = logging.getLogger(__name__)

MATCHED_METHODS = frozenset({"pair", "pair_swapped"})


@dataclass
class GameMatch:
    ot_game_num: str
    espn_game_id: str | None
    away_team_id: str | None
    home_team_id: str | None
    method: str
    orientation_swapped: bool = False
    kickoff_delta_minutes: float | None = None
    ot_kickoff_utc: str | None = None
    espn_kickoff_utc: str | None = None
    away_resolution: str | None = None
    home_resolution: str | None = None
    notes: list[str] = field(default_factory=list)

    def as_row(self, snapshot_utc: str) -> dict[str, Any]:
        return {"snapshot_utc": snapshot_utc, **self.__dict__}


def _delta_minutes(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return abs((a - b).total_seconds()) / 60.0


def _index_espn_games(games: list[dict[str, Any]]) -> dict[frozenset[str], list[dict[str, Any]]]:
    idx: dict[frozenset[str], list[dict[str, Any]]] = {}
    for g in games:
        if g.get("home_team_id") and g.get("away_team_id"):
            idx.setdefault(frozenset({g["home_team_id"], g["away_team_id"]}), []).append(g)
    return idx


def match_game(
    ot_game: dict[str, Any],
    away: ResolvedTeam,
    home: ResolvedTeam,
    espn_index: dict[frozenset[str], list[dict[str, Any]]],
    window_hours: float,
) -> GameMatch:
    m = GameMatch(
        ot_game_num=ot_game["ot_game_num"],
        espn_game_id=None,
        away_team_id=away.team_id,
        home_team_id=home.team_id,
        method="unmatched",
        ot_kickoff_utc=ot_game.get("kickoff_utc"),
        away_resolution=away.method,
        home_resolution=home.method,
    )
    if not (away.resolved and home.resolved):
        m.method = "unresolved_team"
        for side, r in (("away", away), ("home", home)):
            if not r.resolved:
                m.notes.append(
                    f"{side} '{r.raw_name}' {r.method}" + (f" candidates={r.candidates}" if r.candidates else "")
                )
        return m

    candidates = espn_index.get(frozenset({away.team_id, home.team_id}), [])  # type: ignore[arg-type]
    if not candidates:
        m.method = "no_espn_game_for_pair"
        return m

    ot_kick = parse_espn_datetime(ot_game.get("kickoff_utc"))
    scored: list[tuple[float, bool, dict[str, Any]]] = []
    for g in candidates:
        swapped = not (g["away_team_id"] == away.team_id and g["home_team_id"] == home.team_id)
        delta = _delta_minutes(ot_kick, parse_espn_datetime(g.get("kickoff_utc")))
        scored.append((delta if delta is not None else float("inf"), swapped, g))
    # nearest kickoff first; same orientation wins ties
    scored.sort(key=lambda t: (t[0], t[1]))
    delta, swapped, best = scored[0]

    m.espn_game_id = best["game_id"]
    m.espn_kickoff_utc = best.get("kickoff_utc")
    m.orientation_swapped = swapped
    m.kickoff_delta_minutes = None if delta == float("inf") else round(delta, 1)
    if delta != float("inf") and delta > window_hours * 60:
        m.method = "pair_match_outside_window"
        m.notes.append(f"kickoff delta {delta:.0f} min > {window_hours}h window")
    else:
        m.method = "pair_swapped" if swapped else "pair"
        if swapped:
            m.notes.append("home/away orientation differs (neutral site?)")
        if delta != float("inf") and delta > 0:
            m.notes.append(f"kickoff delta {delta:.0f} min")
    return m


def join_overtime_to_espn(
    ot_games: list[dict[str, Any]],
    espn_games: list[dict[str, Any]],
    resolver: TeamResolver,
    window_hours: float,
    snapshot_utc: str | None = None,
) -> dict[str, Any]:
    """Produce the game map + team resolution table for one Overtime snapshot."""
    snapshot_utc = snapshot_utc or iso_utc(utc_now())
    espn_index = _index_espn_games(espn_games)
    matches: list[GameMatch] = []
    resolutions: dict[str, ResolvedTeam] = {}
    for g in ot_games:
        away = resolutions.setdefault(g["away_team_raw"], resolver.resolve(g["away_team_raw"]))
        home = resolutions.setdefault(g["home_team_raw"], resolver.resolve(g["home_team_raw"]))
        matches.append(match_game(g, away, home, espn_index, window_hours))

    team_rows = [
        {
            "snapshot_utc": snapshot_utc,
            "source": "overtime_ag",
            "raw_name": r.raw_name,
            "team_id": r.team_id,
            "espn_display_name": r.espn_display_name,
            "method": r.method,
            "candidates": r.candidates,
        }
        for r in sorted(resolutions.values(), key=lambda r: r.raw_name)
    ]
    summary = {
        "games": len(matches),
        "matched": sum(1 for m in matches if m.method in MATCHED_METHODS),
        "swapped": sum(1 for m in matches if m.orientation_swapped),
        "unresolved_team": sum(1 for m in matches if m.method == "unresolved_team"),
        "no_pair": sum(1 for m in matches if m.method == "no_espn_game_for_pair"),
        "outside_window": sum(1 for m in matches if m.method == "pair_match_outside_window"),
        "teams_unresolved": [r["raw_name"] for r in team_rows if r["team_id"] is None],
    }
    log.info("game join: %s", summary)
    return {
        "meta": {"snapshot_utc": snapshot_utc, "window_hours": window_hours, **summary},
        "game_map": [m.as_row(snapshot_utc) for m in matches],
        "team_map": team_rows,
    }
