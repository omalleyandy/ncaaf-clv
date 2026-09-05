"""Command-line entry point. Business logic lives in the package modules; this only wires args.

ncaaf-clv espn      [--date YYYYMMDD ...] [--snapshot TAG] [--no-games] [--no-teams]
ncaaf-clv overtime  RAW_DUMP.json [--snapshot TAG]
ncaaf-clv join      --espn-snapshot TAG --odds ODDS.json
ncaaf-clv validate  --espn-snapshot TAG --odds ODDS.json --join JOIN.json
ncaaf-clv run       RAW_DUMP.json [--snapshot TAG]        # espn + overtime + join + validate
ncaaf-clv load-db   --db PATH --espn-snapshot TAG [--odds ODDS.json] [--join JOIN.json] [--report REPORT.json]
ncaaf-clv backfill  --years 2021-2025 [--weeks 1-15] [--season-type 2] [--db PATH] [--keep-raw] [--force]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from ncaaf_clv import storage
from ncaaf_clv.common import read_json, snapshot_tag, write_json
from ncaaf_clv.config import REGULAR_SEASON_TYPE, Settings, configure_logging, load_settings
from ncaaf_clv.espn.collect import run_backfill_pipeline, run_espn_pipeline
from ncaaf_clv.overtime.parse import parse_overtime_dump
from ncaaf_clv.resolve.game_join import join_overtime_to_espn
from ncaaf_clv.resolve.team_resolver import TeamResolver
from ncaaf_clv.validate.validator import validate_snapshot

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ step functions


def step_overtime(settings: Settings, raw_path: Path, snapshot: str) -> Path:
    raw = read_json(raw_path)
    parsed = parse_overtime_dump(raw, settings.overtime_tz)
    parsed["meta"]["snapshot"] = snapshot
    parsed["meta"]["raw_file"] = str(raw_path)
    out = settings.processed_dir / "overtime" / snapshot / "ncaaf_odds.json"
    write_json(out, parsed)
    log.info("overtime: %d lines / %d games -> %s", parsed["meta"]["line_count"], parsed["meta"]["game_count"], out)
    return out


def step_join(settings: Settings, espn_snapshot: str, odds_path: Path, snapshot: str) -> Path:
    espn_dir = settings.processed_dir / "espn" / espn_snapshot
    resolver = TeamResolver.from_files(espn_dir / "teams.json")
    odds = read_json(odds_path)
    espn_games = read_json(espn_dir / "scoreboard.json")["games"]
    result = join_overtime_to_espn(
        odds["games"], espn_games, resolver, settings.join_window_hours, odds["meta"]["snapshot_utc"]
    )
    result["meta"].update({"snapshot": snapshot, "espn_snapshot": espn_snapshot, "odds_file": str(odds_path)})
    out = settings.processed_dir / "joins" / snapshot / "game_map.json"
    write_json(out, result)
    return out


def step_validate(settings: Settings, espn_snapshot: str, odds_path: Path, join_path: Path, snapshot: str) -> Path:
    espn_dir = settings.processed_dir / "espn" / espn_snapshot
    odds = read_json(odds_path)
    join = read_json(join_path)
    report = validate_snapshot(espn_dir, odds, join, odds["meta"]["snapshot_utc"])
    report["meta"].update({"snapshot": snapshot, "espn_snapshot": espn_snapshot})
    out = settings.processed_dir / "validation" / snapshot / "report.json"
    write_json(out, report)
    log.info(
        "validation: %s (%d errors, %d warnings)",
        report["meta"]["status"],
        report["meta"]["errors"],
        report["meta"]["warnings"],
    )
    return out


# ----------------------------------------------------------------------- commands


def cmd_espn(args: argparse.Namespace, settings: Settings) -> int:
    manifest = asyncio.run(run_espn_pipeline(settings, args.date, not args.no_games, not args.no_teams, args.snapshot))
    print(json.dumps(manifest, indent=2))
    return 0


def cmd_overtime(args: argparse.Namespace, settings: Settings) -> int:
    print(step_overtime(settings, Path(args.raw), args.snapshot or snapshot_tag()))
    return 0


def cmd_join(args: argparse.Namespace, settings: Settings) -> int:
    out = step_join(settings, args.espn_snapshot, Path(args.odds), args.snapshot or args.espn_snapshot)
    print(json.dumps(read_json(out)["meta"], indent=2))
    return 0


def cmd_validate(args: argparse.Namespace, settings: Settings) -> int:
    out = step_validate(
        settings, args.espn_snapshot, Path(args.odds), Path(args.join), args.snapshot or args.espn_snapshot
    )
    report = read_json(out)
    print(json.dumps(report["meta"], indent=2))
    for c in report["checks"]:
        if not c["passed"]:
            print(
                f"[{c['level'].upper():5s}] {c['name']}: {c['failure_count']} of {c['observed']} - {c['description']}"
            )
    return 1 if report["meta"]["status"] == "FAIL" else 0


def cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    snapshot = args.snapshot or snapshot_tag()
    manifest = asyncio.run(run_espn_pipeline(settings, args.date, True, True, snapshot))
    odds = step_overtime(settings, Path(args.raw), snapshot)
    join = step_join(settings, snapshot, odds, snapshot)
    report = step_validate(settings, snapshot, odds, join, snapshot)
    print(json.dumps({"espn": manifest, "odds": str(odds), "join": str(join), "report": str(report)}, indent=2))
    return 1 if read_json(report)["meta"]["status"] == "FAIL" else 0


def cmd_load_db(args: argparse.Namespace, settings: Settings) -> int:
    conn = storage.connect(Path(args.db))
    counts = storage.load_espn_snapshot(conn, settings.processed_dir / "espn" / args.espn_snapshot)
    if args.odds:
        counts["overtime_odds"] = storage.load_overtime_snapshot(conn, Path(args.odds))
    if args.join:
        counts.update(storage.load_join(conn, Path(args.join)))
    if args.report:
        counts["validation_checks"] = storage.load_validation(conn, Path(args.report))
    conn.close()
    print(json.dumps(counts, indent=2))
    return 0


def parse_int_ranges(spec: str) -> list[int]:
    """'2021-2025' / '1,3,5-7' -> sorted distinct ints."""
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.update(range(int(lo), int(hi) + 1))
        else:
            out.add(int(part))
    if not out:
        raise argparse.ArgumentTypeError(f"empty range spec: {spec!r}")
    return sorted(out)


def cmd_backfill(args: argparse.Namespace, settings: Settings) -> int:
    years = parse_int_ranges(args.years)
    weeks = parse_int_ranges(args.weeks)
    conn = storage.connect(Path(args.db)) if args.db else None

    def _season_done(out_dir: Path, manifest: dict) -> None:
        if conn is None:
            return
        manifest["db_counts"] = storage.load_espn_snapshot(conn, out_dir)
        log.info("loaded %s into %s: %s", out_dir, args.db, manifest["db_counts"])

    try:
        manifests = asyncio.run(
            run_backfill_pipeline(settings, years, weeks, args.season_type, args.keep_raw, args.force, _season_done)
        )
    finally:
        if conn is not None:
            conn.close()
    print(json.dumps(manifests, indent=2))
    return 1 if any(m["games_failed"] for m in manifests) else 0


# ------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ncaaf-clv", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--env", help="path to .env file", default=None)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("espn", help="collect ESPN scoreboard, game summaries and team pages")
    s.add_argument("--date", action="append", help="YYYYMMDD (repeatable); default = ESPN current week")
    s.add_argument("--snapshot", help="snapshot tag (default: now)")
    s.add_argument("--no-games", action="store_true")
    s.add_argument("--no-teams", action="store_true")
    s.set_defaults(func=cmd_espn)

    s = sub.add_parser("overtime", help="normalise an Overtime.ag raw capture")
    s.add_argument("raw")
    s.add_argument("--snapshot")
    s.set_defaults(func=cmd_overtime)

    s = sub.add_parser("join", help="map Overtime games to ESPN games")
    s.add_argument("--espn-snapshot", required=True)
    s.add_argument("--odds", required=True)
    s.add_argument("--snapshot")
    s.set_defaults(func=cmd_join)

    s = sub.add_parser("validate", help="validate games, teams and odds")
    s.add_argument("--espn-snapshot", required=True)
    s.add_argument("--odds", required=True)
    s.add_argument("--join", required=True)
    s.add_argument("--snapshot")
    s.set_defaults(func=cmd_validate)

    s = sub.add_parser("run", help="espn + overtime + join + validate in one snapshot")
    s.add_argument("raw", help="Overtime.ag raw capture JSON")
    s.add_argument("--date", action="append")
    s.add_argument("--snapshot")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("load-db", help="load processed JSON into SQLite")
    s.add_argument("--db", required=True)
    s.add_argument("--espn-snapshot", required=True)
    s.add_argument("--odds")
    s.add_argument("--join")
    s.add_argument("--report")
    s.set_defaults(func=cmd_load_db)

    s = sub.add_parser("backfill", help="collect past seasons by (year, week) incl. core-API sportsbook lines")
    s.add_argument("--years", required=True, help="e.g. 2021-2025 or 2023,2025")
    s.add_argument("--weeks", default="1-15", help="default 1-15 (regular season incl. championship week)")
    s.add_argument("--season-type", type=int, default=REGULAR_SEASON_TYPE, help="2 = regular, 3 = postseason")
    s.add_argument("--db", help="load each season into this SQLite file as it completes")
    s.add_argument("--keep-raw", action="store_true", help="persist raw ESPN payloads under data/raw/espn/")
    s.add_argument("--force", action="store_true", help="re-fetch games already present in the snapshot dir")
    s.set_defaults(func=cmd_backfill)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings(args.env)
    configure_logging(settings.log_level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return args.func(args, settings)


if __name__ == "__main__":
    sys.exit(main())
