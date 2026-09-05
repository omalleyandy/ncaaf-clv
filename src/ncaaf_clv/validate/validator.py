"""Validation of an ESPN snapshot + Overtime odds snapshot + their join.

Produces a report of checks at three levels:
    error  - data is wrong or unjoinable (fails the run)
    warn   - suspicious; review before modelling
    info   - expected/explainable gaps (e.g. FCS opponents not in the FBS dimension)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ncaaf_clv.common import iso_utc, read_json, utc_now
from ncaaf_clv.resolve.game_join import MATCHED_METHODS

log = logging.getLogger(__name__)

MAX_SPREAD = 60.0
TOTAL_RANGE = {"game": (20.0, 110.0), "1h": (8.0, 60.0), "1q": (3.0, 35.0), "2h": (8.0, 60.0)}
PRICE_RANGE = (-400, 400)  # spread / total juice outside this is almost certainly a parse error
TEAM_TOTAL_SUM_TOL = 3.0
CROSS_BOOK_SPREAD_TOL = 3.5
CROSS_BOOK_TOTAL_TOL = 4.0
KICKOFF_WARN_MINUTES = 60.0


@dataclass
class Check:
    name: str
    level: str
    description: str
    failures: list[dict[str, Any]] = field(default_factory=list)
    observed: int = 0

    @property
    def passed(self) -> bool:
        return not self.failures

    def add(self, **detail: Any) -> None:
        self.failures.append(detail)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "level": self.level,
            "description": self.description,
            "passed": self.passed,
            "observed": self.observed,
            "failure_count": len(self.failures),
            "failures": self.failures[:200],
        }


def _sign(x: float | int | None) -> int:
    if x is None:
        return 0
    return (x > 0) - (x < 0)


# ----------------------------------------------------------------------------- loaders


def load_espn_snapshot(espn_dir: Path) -> dict[str, Any]:
    scoreboard = read_json(espn_dir / "scoreboard.json")
    teams = read_json(espn_dir / "teams.json")
    games_index = read_json(espn_dir / "games" / "_index.json") if (espn_dir / "games" / "_index.json").exists() else {}
    return {"scoreboard": scoreboard, "teams": teams, "games_index": games_index, "dir": espn_dir}


# ------------------------------------------------------------------------------ checks


def check_espn_internal(espn: dict[str, Any]) -> list[Check]:
    sb, dims = espn["scoreboard"], espn["teams"]
    program_ids = {t["team_id"] for t in dims["teams"] if t.get("is_program", True)}
    games = sb["games"]
    checks: list[Check] = []

    c = Check("espn_game_ids_unique", "error", "scoreboard game_id values are unique")
    seen: set[str] = set()
    for g in games:
        c.observed += 1
        if g["game_id"] in seen:
            c.add(game_id=g["game_id"])
        seen.add(g["game_id"])
    checks.append(c)

    c = Check("espn_game_has_both_teams", "error", "every game has home_team_id and away_team_id")
    for g in games:
        c.observed += 1
        if not (g.get("home_team_id") and g.get("away_team_id")):
            c.add(game_id=g["game_id"], name=g.get("name"))
    checks.append(c)

    c = Check(
        "espn_non_fbs_opponents", "info", "game teams not in the FBS dimension (FCS opponents expected in early weeks)"
    )
    for gt in sb["game_teams"]:
        c.observed += 1
        if gt["team_id"] not in program_ids:
            c.add(game_id=gt["game_id"], team_id=gt["team_id"], team=gt.get("team_display_name"))
    checks.append(c)

    c = Check("espn_game_files_present", "error", "gamecast/boxscore/play_by_play files exist for each scoreboard game")
    for g in games:
        c.observed += 1
        gdir = espn["dir"] / "games" / g["game_id"]
        missing = [f for f in ("gamecast.json", "boxscore.json", "play_by_play.json") if not (gdir / f).exists()]
        if missing:
            c.add(game_id=g["game_id"], missing=missing)
    checks.append(c)

    c = Check("espn_final_games_have_stats", "warn", "FINAL games have box score player rows and plays")
    c2 = Check("espn_boxscore_ids_consistent", "error", "box score team ids belong to the game; player ids non-null")
    for g in games:
        gdir = espn["dir"] / "games" / g["game_id"]
        if not (gdir / "boxscore.json").exists():
            continue
        box = read_json(gdir / "boxscore.json")
        pbp = read_json(gdir / "play_by_play.json")
        if g.get("status_name") == "STATUS_FINAL":
            c.observed += 1
            if not box["player_stats"] or not pbp["plays"]:
                c.add(game_id=g["game_id"], player_rows=len(box["player_stats"]), plays=len(pbp["plays"]))
        team_ids = {g["home_team_id"], g["away_team_id"]}
        for row in box["player_stats"]:
            c2.observed += 1
            if row["team_id"] not in team_ids or not row.get("player_id"):
                c2.add(game_id=g["game_id"], team_id=row["team_id"], player_id=row.get("player_id"))
                break
    checks.extend([c, c2])

    c = Check("espn_team_pages_present", "warn", "statistics/schedule/roster files exist for each FBS program")
    for tid in sorted(program_ids, key=int):
        c.observed += 1
        tdir = espn["dir"] / "teams" / tid
        missing = [f for f in ("statistics.json", "schedule.json", "roster.json") if not (tdir / f).exists()]
        if missing:
            c.add(team_id=tid, missing=missing)
    checks.append(c)
    return checks


def check_overtime_internal(odds: list[dict[str, Any]], ot_games: list[dict[str, Any]]) -> list[Check]:
    checks: list[Check] = []
    by_game: dict[str, dict[str, dict[str, Any]]] = {}
    for r in odds:
        by_game.setdefault(r["ot_game_num"], {})[r["period_key"]] = r

    c = Check("ot_one_row_per_game_period", "error", "no duplicate (game, period) rows")
    seen: set[tuple[str, str]] = set()
    for r in odds:
        c.observed += 1
        key = (r["ot_game_num"], r["period_key"])
        if key in seen:
            c.add(ot_game_num=r["ot_game_num"], period=r["period_key"])
        seen.add(key)
    checks.append(c)

    c = Check("ot_game_period_present", "warn", "each game has a full-game line (1h/1q may be absent)")
    for gnum, periods in by_game.items():
        c.observed += 1
        if "game" not in periods:
            c.add(ot_game_num=gnum, periods=sorted(periods))
    checks.append(c)

    c = Check(
        "ot_spread_favorite_consistent",
        "error",
        f"favorite_side set whenever spread != 0; magnitude <= {MAX_SPREAD:.0f}",
    )
    for r in odds:
        c.observed += 1
        mag = r.get("spread_magnitude")
        if mag is None:
            continue
        if mag > MAX_SPREAD or (mag > 0 and r.get("favorite_side") not in ("home", "away")):
            c.add(
                ot_game_num=r["ot_game_num"], period=r["period_key"], spread=mag, favorite_side=r.get("favorite_side")
            )
    checks.append(c)

    c = Check("ot_moneyline_matches_favorite", "warn", "favourite's moneyline is the shorter price")
    for r in odds:
        hm, am = r.get("home_ml"), r.get("away_ml")
        if hm is None or am is None or not r.get("favorite_side"):
            continue
        c.observed += 1
        fav_ml, dog_ml = (hm, am) if r["favorite_side"] == "home" else (am, hm)
        if (r.get("spread_magnitude") or 0) > 0 and not (fav_ml < dog_ml):
            c.add(
                ot_game_num=r["ot_game_num"],
                period=r["period_key"],
                home_ml=hm,
                away_ml=am,
                favorite=r["favorite_side"],
            )
    checks.append(c)

    c = Check("ot_total_in_range", "warn", "totals within plausible range per period")
    for r in odds:
        tot = r.get("total")
        rng = TOTAL_RANGE.get(r["period_key"])
        if tot is None or rng is None:
            continue
        c.observed += 1
        if not (rng[0] <= tot <= rng[1]):
            c.add(ot_game_num=r["ot_game_num"], period=r["period_key"], total=tot)
    checks.append(c)

    c = Check("ot_prices_in_range", "error", f"spread/total/team-total prices within {PRICE_RANGE}")
    price_fields = (
        "away_spread_price",
        "home_spread_price",
        "over_price",
        "under_price",
        "away_tt_over_price",
        "away_tt_under_price",
        "home_tt_over_price",
        "home_tt_under_price",
    )
    for r in odds:
        for f in price_fields:
            v = r.get(f)
            if v is None:
                continue
            c.observed += 1
            if not (PRICE_RANGE[0] <= v <= PRICE_RANGE[1]) or v == 0:
                c.add(ot_game_num=r["ot_game_num"], period=r["period_key"], field=f, value=v)
    checks.append(c)

    c = Check(
        "ot_team_totals_sum_to_total", "warn", f"home+away team totals within {TEAM_TOTAL_SUM_TOL} of the game total"
    )
    for r in odds:
        ht, at, tot = r.get("home_team_total"), r.get("away_team_total"), r.get("total")
        if None in (ht, at, tot):
            continue
        c.observed += 1
        if abs((ht + at) - tot) > TEAM_TOTAL_SUM_TOL:
            c.add(ot_game_num=r["ot_game_num"], period=r["period_key"], home_tt=ht, away_tt=at, total=tot)
    checks.append(c)

    c = Check(
        "ot_period_lines_monotone", "warn", "1q total < 1h total < game total; period favourite matches game favourite"
    )
    for gnum, periods in by_game.items():
        game, h1, q1 = periods.get("game"), periods.get("1h"), periods.get("1q")
        if not game:
            continue
        c.observed += 1
        problems = []
        if h1 and game.get("total") and h1.get("total") and not h1["total"] < game["total"]:
            problems.append("1h_total>=game_total")
        if h1 and q1 and h1.get("total") and q1.get("total") and not q1["total"] < h1["total"]:
            problems.append("1q_total>=1h_total")
        for label, p in (("1h", h1), ("1q", q1)):
            if (
                p
                and p.get("favorite_side")
                and game.get("favorite_side")
                and p["favorite_side"] != game["favorite_side"]
            ):
                problems.append(f"{label}_favorite_differs")
        if problems:
            c.add(ot_game_num=gnum, problems=problems)
    checks.append(c)

    c = Check("ot_kickoff_parsed", "error", "every game has a UTC kickoff")
    for g in ot_games:
        c.observed += 1
        if not g.get("kickoff_utc"):
            c.add(ot_game_num=g["ot_game_num"], kickoff_local=g.get("kickoff_local"))
    checks.append(c)
    return checks


def check_join(join: dict[str, Any], espn: dict[str, Any], odds: list[dict[str, Any]]) -> list[Check]:
    checks: list[Check] = []
    game_map = join["game_map"]
    espn_games = {g["game_id"]: g for g in espn["scoreboard"]["games"]}
    espn_odds = {o["game_id"]: o for o in espn["scoreboard"]["espn_odds"]}

    c = Check("join_teams_resolved", "error", "every Overtime team name resolves to an ESPN team id")
    for t in join["team_map"]:
        c.observed += 1
        if not t["team_id"]:
            c.add(raw_name=t["raw_name"], method=t["method"], candidates=t.get("candidates"))
    checks.append(c)

    c = Check("join_games_matched", "error", "every Overtime game maps to an ESPN game")
    for m in game_map:
        c.observed += 1
        if m["method"] not in MATCHED_METHODS:
            c.add(ot_game_num=m["ot_game_num"], method=m["method"], notes=m.get("notes"))
    checks.append(c)

    c = Check("join_espn_game_unique", "error", "no ESPN game is claimed by two Overtime games")
    claimed: dict[str, str] = {}
    for m in game_map:
        gid = m.get("espn_game_id")
        if not gid:
            continue
        c.observed += 1
        if gid in claimed:
            c.add(espn_game_id=gid, ot_game_nums=[claimed[gid], m["ot_game_num"]])
        claimed[gid] = m["ot_game_num"]
    checks.append(c)

    c = Check("join_orientation", "warn", "home/away orientation agrees between sources (swapped => neutral site?)")
    for m in game_map:
        if not m.get("espn_game_id"):
            continue
        c.observed += 1
        if m.get("orientation_swapped"):
            g = espn_games.get(m["espn_game_id"], {})
            c.add(ot_game_num=m["ot_game_num"], espn_game_id=m["espn_game_id"], neutral_site=g.get("neutral_site"))
    checks.append(c)

    c = Check("join_kickoff_agreement", "warn", f"kickoff within {KICKOFF_WARN_MINUTES:.0f} min between sources")
    for m in game_map:
        if m.get("kickoff_delta_minutes") is None:
            continue
        c.observed += 1
        if m["kickoff_delta_minutes"] > KICKOFF_WARN_MINUTES:
            g = espn_games.get(m["espn_game_id"], {})
            c.add(
                ot_game_num=m["ot_game_num"],
                espn_game_id=m["espn_game_id"],
                delta_minutes=m["kickoff_delta_minutes"],
                ot_kickoff_utc=m.get("ot_kickoff_utc"),
                espn_kickoff_utc=m.get("espn_kickoff_utc"),
                espn_status=g.get("status_name"),
            )
    checks.append(c)

    c = Check(
        "join_cross_book_agreement",
        "warn",
        "Overtime full-game line vs ESPN/DraftKings: same favourite, spread within "
        f"{CROSS_BOOK_SPREAD_TOL}, total within {CROSS_BOOK_TOTAL_TOL}",
    )
    game_lines = {r["ot_game_num"]: r for r in odds if r["period_key"] == "game"}
    for m in game_map:
        gid = m.get("espn_game_id")
        line = game_lines.get(m["ot_game_num"])
        ref = espn_odds.get(gid or "")
        if not (gid and line and ref):
            continue
        c.observed += 1
        problems: list[str] = []
        # ESPN home_spread is home perspective; translate Overtime to ESPN's home side.
        ot_home_spread = line.get("home_spread")
        if m.get("orientation_swapped") and ot_home_spread is not None:
            ot_home_spread = -ot_home_spread
        if ot_home_spread is not None and ref.get("home_spread") is not None:
            if _sign(ot_home_spread) != _sign(ref["home_spread"]) and abs(ot_home_spread) > 0.5:
                problems.append(f"favorite differs (ot home {ot_home_spread:+}, espn home {ref['home_spread']:+})")
            elif abs(ot_home_spread - ref["home_spread"]) > CROSS_BOOK_SPREAD_TOL:
                problems.append(f"spread gap {abs(ot_home_spread - ref['home_spread']):.1f}")
        if (
            line.get("total") is not None
            and ref.get("total") is not None
            and abs(line["total"] - ref["total"]) > CROSS_BOOK_TOTAL_TOL
        ):
            problems.append(f"total gap {abs(line['total'] - ref['total']):.1f}")
        if problems:
            c.add(
                ot_game_num=m["ot_game_num"],
                espn_game_id=gid,
                problems=problems,
                ot={"home_spread": ot_home_spread, "total": line.get("total")},
                espn={
                    "home_spread": ref.get("home_spread"),
                    "total": ref.get("total"),
                    "provider": ref.get("provider_name"),
                },
            )
    checks.append(c)

    c = Check(
        "join_espn_game_not_started",
        "info",
        "matched ESPN game status at snapshot (lines on in-progress games are live lines)",
    )
    for m in game_map:
        g = espn_games.get(m.get("espn_game_id") or "")
        if not g:
            continue
        c.observed += 1
        if g.get("status_state") != "pre":
            c.add(ot_game_num=m["ot_game_num"], espn_game_id=g["game_id"], status=g.get("status_name"))
    checks.append(c)
    return checks


# ------------------------------------------------------------------------------ report


def build_report(checks: list[Check], snapshot_utc: str | None = None) -> dict[str, Any]:
    snapshot_utc = snapshot_utc or iso_utc(utc_now())
    errors = [c for c in checks if c.level == "error" and not c.passed]
    warns = [c for c in checks if c.level == "warn" and not c.passed]
    return {
        "meta": {
            "snapshot_utc": snapshot_utc,
            "status": "FAIL" if errors else ("WARN" if warns else "PASS"),
            "checks": len(checks),
            "errors": len(errors),
            "warnings": len(warns),
            "infos": sum(1 for c in checks if c.level == "info" and not c.passed),
        },
        "checks": [c.as_dict() for c in checks],
    }


def validate_snapshot(
    espn_dir: Path, odds_payload: dict[str, Any], join_payload: dict[str, Any], snapshot_utc: str | None = None
) -> dict[str, Any]:
    espn = load_espn_snapshot(espn_dir)
    checks = check_espn_internal(espn)
    checks += check_overtime_internal(odds_payload["odds"], odds_payload["games"])
    checks += check_join(join_payload, espn, odds_payload["odds"])
    return build_report(checks, snapshot_utc)
