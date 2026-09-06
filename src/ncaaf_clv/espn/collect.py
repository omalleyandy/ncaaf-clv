"""ESPN collection orchestration: scoreboard -> per-game summary; team list -> per-team pages.

Output layout (under <data_dir>/processed/espn/<snapshot>/):
    scoreboard.json                      games, game_teams, espn_odds
    games/<game_id>/gamecast.json        game, teams, scoring_plays, leaders, win_probability, espn_odds
    games/<game_id>/boxscore.json        team_stats, player_stats
    games/<game_id>/play_by_play.json    drives, plays
    teams.json                           teams (dims) + conferences
    teams/<team_id>/statistics.json      team season stats (EAV)
    teams/<team_id>/schedule.json        schedule rows
    teams/<team_id>/roster.json          players, coaches

Two entry points:
    run_espn_pipeline      current week (scoreboard by date) + every game + every FBS team's pages
    run_backfill_pipeline  past seasons by (year, week); one snapshot per season, resumable, with the
                           core API's per-game sportsbook lines (the summary's pickcenter is empty for
                           seasons before 2023 and carries no open/close before ESPN BET).
    espn.refresh           re-collect the games still open in SQLite into a `refresh-<tag>` snapshot
                           (scoreboard rows for the open set, summaries for those that kicked off).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import httpx

from ncaaf_clv.common import iso_utc, read_json, snapshot_tag, utc_now, write_json
from ncaaf_clv.config import REGULAR_SEASON_TYPE, SOURCE_ESPN, Settings
from ncaaf_clv.espn.client import BlockedError, ESPNClient, group_id_from_ref, team_id_from_ref
from ncaaf_clv.espn.parse import (
    parse_boxscore,
    parse_core_odds,
    parse_core_season_team,
    parse_gamecast,
    parse_play_by_play,
    parse_scoreboard,
    parse_team_detail,
    parse_team_roster,
    parse_team_schedule,
    parse_team_statistics,
)

log = logging.getLogger(__name__)

GAME_FILES = ("gamecast.json", "boxscore.json", "play_by_play.json")
GAME_CHUNK = 60  # games per gather() batch; lets a blocking signal stop the run promptly


def _meta(snapshot_utc: str, snapshot: str, **extra: Any) -> dict[str, Any]:
    return {"source": SOURCE_ESPN, "snapshot_utc": snapshot_utc, "snapshot": snapshot, **extra}


def assign_conference(
    row: dict[str, Any],
    conf_by_id: dict[str, dict[str, Any]],
    parent_of: dict[str, str | None],
    fbs_group_id: int | str,
) -> dict[str, Any]:
    """Resolve a core-API team row's season `group_id` to conference / division columns.

    A team's group is either a conference (a child of the FBS group) or a division whose parent is
    the conference. Teams in neither (all-star squads, unresolvable groups) are `is_program=False`.
    """
    gid = row.pop("group_id", None)
    all_star = bool(row.pop("is_all_star", False))
    conf = conf_by_id.get(gid) if gid else None
    division_id = None
    parent_id: str | None = str(fbs_group_id) if conf else None
    if conf is None and gid and parent_of.get(gid) in conf_by_id:
        parent_id = parent_of[gid]
        conf = conf_by_id[parent_id]  # type: ignore[index]
        division_id = gid
    row.update(
        {
            "division_group_id": division_id,
            "parent_group_id": parent_id,
            "conference_group_id": conf["group_id"] if conf else None,
            "conference_name": conf["name"] if conf else None,
            "conference_abbr": conf["abbreviation"] if conf else None,
            "fbs_group_id": str(fbs_group_id),
            "is_program": conf is not None and not all_star,
        }
    )
    return row


class ESPNCollector:
    def __init__(
        self,
        settings: Settings,
        snapshot: str | None = None,
        snapshot_utc: str | None = None,
        core_odds: bool = False,
    ) -> None:
        self.settings = settings
        now = utc_now()
        self.snapshot = snapshot or snapshot_tag(now)
        self.snapshot_utc = snapshot_utc or iso_utc(now)
        self.core_odds = core_odds
        self.out_dir: Path = settings.processed_dir / "espn" / self.snapshot
        self.raw_dir: Path = settings.raw_espn_dir / self.snapshot

    # ------------------------------------------------------------ scoreboard

    def _merge_scoreboards(self, payloads: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        merged: dict[str, list[dict[str, Any]]] = {"games": [], "game_teams": [], "espn_odds": []}
        seen: set[str] = set()
        for payload in payloads:
            parsed = parse_scoreboard(payload, self.snapshot_utc)
            for game in parsed["games"]:
                if game["game_id"] in seen:
                    continue
                seen.add(game["game_id"])
                merged["games"].append(game)
                merged["game_teams"].extend(r for r in parsed["game_teams"] if r["game_id"] == game["game_id"])
                merged["espn_odds"].extend(r for r in parsed["espn_odds"] if r["game_id"] == game["game_id"])
        return merged

    async def collect_scoreboard(self, client: ESPNClient, dates: list[str] | None = None) -> dict[str, Any]:
        """Fetch the FBS scoreboard for each date (or ESPN's current week if none)."""
        payloads = [await client.scoreboard(d) for d in (dates or [None])]
        merged = self._merge_scoreboards(payloads)
        out = {"meta": _meta(self.snapshot_utc, self.snapshot, dates=dates, game_count=len(merged["games"])), **merged}
        write_json(self.out_dir / "scoreboard.json", out)
        log.info("scoreboard: %d games", len(merged["games"]))
        return out

    async def collect_scoreboard_weeks(
        self, client: ESPNClient, season_year: int, weeks: Iterable[int], season_type: int = REGULAR_SEASON_TYPE
    ) -> dict[str, Any]:
        """Fetch the FBS scoreboard for each (season_year, week) and merge into one scoreboard.json."""
        weeks = list(weeks)
        payloads = []
        for week in weeks:
            payload = await client.scoreboard_week(season_year, week, season_type)
            log.info("scoreboard %d week %d: %d events", season_year, week, len(payload.get("events", [])))
            payloads.append(payload)
        merged = self._merge_scoreboards(payloads)
        out = {
            "meta": _meta(
                self.snapshot_utc,
                self.snapshot,
                season_year=season_year,
                season_type=season_type,
                weeks=weeks,
                game_count=len(merged["games"]),
            ),
            **merged,
        }
        write_json(self.out_dir / "scoreboard.json", out)
        log.info("scoreboard %d weeks %s: %d games", season_year, weeks, len(merged["games"]))
        return out

    async def collect_scoreboard_for_games(
        self,
        client: ESPNClient,
        season_year: int,
        season_weeks: Iterable[tuple[int, int]],
        game_ids: Iterable[str],
    ) -> dict[str, Any]:
        """Fetch the `(season_type, week)` scoreboards and keep only `game_ids` (refresh of open games)."""
        keep = set(game_ids)
        season_weeks = sorted(season_weeks)
        payloads = [await client.scoreboard_week(season_year, week, season_type) for season_type, week in season_weeks]
        merged = self._merge_scoreboards(payloads)
        subset = {key: [r for r in rows if r["game_id"] in keep] for key, rows in merged.items()}
        out = {
            "meta": _meta(
                self.snapshot_utc,
                self.snapshot,
                season_year=season_year,
                weeks=[list(w) for w in season_weeks],
                requested=len(keep),
                game_count=len(subset["games"]),
            ),
            **subset,
        }
        write_json(self.out_dir / "scoreboard.json", out)
        log.info(
            "scoreboard %d weeks %s: %d of %d open games listed",
            season_year,
            season_weeks,
            len(subset["games"]),
            len(keep),
        )
        return out

    # ----------------------------------------------------------------- games

    def game_collected(self, game_id: str) -> bool:
        game_dir = self.out_dir / "games" / game_id
        return all((game_dir / f).exists() for f in GAME_FILES)

    async def _core_odds_rows(self, client: ESPNClient, game: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            items = await client.event_odds(game["game_id"])
        except httpx.HTTPStatusError as exc:  # 404 = ESPN recorded no lines for this game
            log.warning("odds %s: HTTP %d", game["game_id"], exc.response.status_code)
            return []
        return parse_core_odds(items, game["game_id"], game["home_team_id"], game["away_team_id"], self.snapshot_utc)

    async def collect_game(self, client: ESPNClient, game_id: str) -> dict[str, str]:
        payload = await client.summary(game_id)
        game_dir = self.out_dir / "games" / game_id
        gamecast = parse_gamecast(payload, self.snapshot_utc)
        if self.core_odds:
            # Core-API lines (one row per sportsbook) win over pickcenter for the same provider id.
            by_provider = {r["provider_id"]: r for r in gamecast["espn_odds"]}
            by_provider.update({r["provider_id"]: r for r in await self._core_odds_rows(client, gamecast["game"])})
            gamecast["espn_odds"] = list(by_provider.values())
        boxscore = parse_boxscore(payload, self.snapshot_utc)
        pbp = parse_play_by_play(payload, self.snapshot_utc)
        meta = _meta(self.snapshot_utc, self.snapshot, game_id=game_id)
        write_json(game_dir / "gamecast.json", {"meta": meta, **gamecast})
        write_json(game_dir / "boxscore.json", {"meta": meta, **boxscore})
        write_json(game_dir / "play_by_play.json", {"meta": meta, **pbp})
        return {
            "game_id": game_id,
            "status": gamecast["game"].get("status_name"),
            "odds_rows": str(len(gamecast["espn_odds"])),
            "team_stat_rows": str(len(boxscore["team_stats"])),
            "player_stat_rows": str(len(boxscore["player_stats"])),
            "plays": str(len(pbp["plays"])),
        }

    async def collect_games(
        self, client: ESPNClient, game_ids: list[str], skip_existing: bool = False
    ) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        failures: list[str] = []
        skipped = [g for g in game_ids if skip_existing and self.game_collected(g)]
        skipped_set = set(skipped)
        todo = [g for g in game_ids if g not in skipped_set]
        blocked = False

        async def _one(gid: str) -> None:
            nonlocal blocked
            try:
                results.append(await self.collect_game(client, gid))
            except BlockedError:
                blocked = True
                failures.append(gid)
            except Exception:  # noqa: BLE001 - keep collecting other games; failure recorded
                log.exception("game %s failed", gid)
                failures.append(gid)

        for start in range(0, len(todo), GAME_CHUNK):
            if blocked:
                failures.extend(todo[start:])
                log.error("ESPN blocking signal: stopped with %d games not fetched", len(todo) - start)
                break
            await asyncio.gather(*(_one(g) for g in todo[start : start + GAME_CHUNK]))
            log.info("games: %d/%d fetched (%d failed)", len(results), len(todo), len(failures))
        write_json(
            self.out_dir / "games" / "_index.json",
            {
                "meta": _meta(self.snapshot_utc, self.snapshot),
                "collected": results,
                "failed": failures,
                "skipped_existing": skipped,
                "blocked": blocked,
            },
        )
        log.info("games: %d collected, %d skipped (existing), %d failed", len(results), len(skipped), len(failures))
        if blocked:
            raise BlockedError(f"ESPN blocked the run; {len(failures)} games missing (re-run to resume)")
        return results

    # ----------------------------------------------------------------- teams

    async def collect_team_list(self, client: ESPNClient) -> dict[str, Any]:
        """FBS team dimension table + conference groups (from the core API group 80)."""
        conferences = await client.group_children(self.settings.season_year)
        conf_by_id = {c["group_id"]: c for c in conferences}
        refs = await client.group_team_refs(self.settings.season_year)
        team_ids = sorted({team_id_from_ref(r) for r in refs}, key=int)
        details = await asyncio.gather(*(client.team_detail(t) for t in team_ids))
        teams = []
        for payload in details:
            row = parse_team_detail(payload, self.snapshot_utc)
            # Some conferences have divisions (e.g. Sun Belt East/West): the team's group is the
            # division and its parent is the conference.
            group_id = row.get("conference_group_id") or ""
            conf = conf_by_id.get(group_id)
            division_id = None
            if conf is None and row.get("parent_group_id") in conf_by_id:
                conf = conf_by_id[row["parent_group_id"]]
                division_id = group_id
            row["division_group_id"] = division_id
            row["conference_group_id"] = conf["group_id"] if conf else None
            row["conference_name"] = conf["name"] if conf else None
            row["conference_abbr"] = conf["abbreviation"] if conf else None
            row["fbs_group_id"] = str(self.settings.espn_group_id)
            # ESPN lists all-star / exhibition squads inside group 80; they have no conference.
            row["is_program"] = conf is not None
            teams.append(row)
        out = {
            "meta": _meta(
                self.snapshot_utc, self.snapshot, group_id=self.settings.espn_group_id, team_count=len(teams)
            ),
            "teams": teams,
            "conferences": conferences,
        }
        write_json(self.out_dir / "teams.json", out)
        log.info("teams: %d FBS teams, %d conferences", len(teams), len(conferences))
        return out

    async def collect_season_team_list(self, client: ESPNClient, season_year: int) -> dict[str, Any]:
        """FBS membership and conference alignment *as of `season_year`* (core API season objects).

        The site API's team page only knows the current season, so historical snapshots use
        `seasons/<year>/teams/<id>` instead; `is_program` excludes all-star squads."""
        conferences = await client.group_children(season_year)
        conf_by_id = {c["group_id"]: c for c in conferences}
        refs = await client.group_team_refs(season_year)
        team_ids = sorted({team_id_from_ref(r) for r in refs}, key=int)
        payloads = await asyncio.gather(*(client.season_team(season_year, t) for t in team_ids))
        rows = [parse_core_season_team(p, self.snapshot_utc) for p in payloads]
        # Division groups (SEC West, Sun Belt East, ...) are not conferences: fetch each once for its parent.
        ref_by_gid = {group_id_from_ref((p.get("groups") or {}).get("$ref") or ""): p for p in payloads}
        parent_of: dict[str, str | None] = {}
        for gid in sorted({r["group_id"] for r in rows if r["group_id"] and r["group_id"] not in conf_by_id}):
            group = await client.group_by_ref(ref_by_gid[gid]["groups"]["$ref"])
            parent_of[gid] = group_id_from_ref((group.get("parent") or {}).get("$ref") or "")
        teams = [assign_conference(r, conf_by_id, parent_of, self.settings.espn_group_id) for r in rows]
        out = {
            "meta": _meta(
                self.snapshot_utc,
                self.snapshot,
                group_id=self.settings.espn_group_id,
                season_year=season_year,
                team_count=len(teams),
            ),
            "teams": teams,
            "conferences": conferences,
        }
        write_json(self.out_dir / "teams.json", out)
        programs = sum(1 for t in teams if t["is_program"])
        log.info(
            "teams %d: %d in group, %d programs, %d conferences", season_year, len(teams), programs, len(conferences)
        )
        return out

    async def collect_team(self, client: ESPNClient, team_id: str) -> dict[str, Any]:
        stats_p, sched_p, roster_p = await asyncio.gather(
            client.team_statistics(team_id),
            client.team_schedule(team_id, self.settings.season_year),
            client.team_roster(team_id),
        )
        team_dir = self.out_dir / "teams" / team_id
        meta = _meta(self.snapshot_utc, self.snapshot, team_id=team_id)
        stats = parse_team_statistics(stats_p, self.snapshot_utc)
        sched = parse_team_schedule(sched_p, self.snapshot_utc)
        roster = parse_team_roster(roster_p, self.snapshot_utc)
        write_json(team_dir / "statistics.json", {"meta": meta, "statistics": stats})
        write_json(team_dir / "schedule.json", {"meta": meta, "schedule": sched})
        write_json(team_dir / "roster.json", {"meta": meta, **roster})
        return {
            "team_id": team_id,
            "stat_rows": len(stats),
            "schedule_rows": len(sched),
            "players": len(roster["players"]),
        }

    async def collect_teams(self, client: ESPNClient, team_ids: list[str]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        failures: list[str] = []

        async def _one(tid: str) -> None:
            try:
                results.append(await self.collect_team(client, tid))
            except Exception:  # noqa: BLE001
                log.exception("team %s failed", tid)
                failures.append(tid)

        await asyncio.gather(*(_one(t) for t in team_ids))
        write_json(
            self.out_dir / "teams" / "_index.json",
            {"meta": _meta(self.snapshot_utc, self.snapshot), "collected": results, "failed": failures},
        )
        log.info("team pages: %d collected, %d failed", len(results), len(failures))
        return results


async def run_espn_pipeline(
    settings: Settings,
    dates: list[str] | None = None,
    include_games: bool = True,
    include_teams: bool = True,
    snapshot: str | None = None,
) -> dict[str, Any]:
    """Full ESPN pass. Returns a manifest with paths and counts."""
    collector = ESPNCollector(settings, snapshot)
    async with ESPNClient(settings, raw_dir=collector.raw_dir) as client:
        scoreboard = await collector.collect_scoreboard(client, dates)
        game_ids = [g["game_id"] for g in scoreboard["games"]]
        games_summary = await collector.collect_games(client, game_ids) if include_games else []
        team_dim = await collector.collect_team_list(client)
        team_ids = [t["team_id"] for t in team_dim["teams"] if t["is_program"]]
        teams_summary = await collector.collect_teams(client, team_ids) if include_teams else []
    manifest = {
        "meta": _meta(collector.snapshot_utc, collector.snapshot),
        "out_dir": str(collector.out_dir),
        "games": len(games_summary),
        "teams": len(teams_summary),
        "team_dim": len(team_dim["teams"]),
    }
    write_json(collector.out_dir / "manifest.json", manifest)
    return manifest


def backfill_snapshot(season_year: int) -> str:
    return f"backfill-{season_year}"


async def run_backfill_pipeline(
    settings: Settings,
    season_years: Iterable[int],
    weeks: Iterable[int],
    season_type: int = REGULAR_SEASON_TYPE,
    keep_raw: bool = False,
    force: bool = False,
    on_season_done: Callable[[Path, dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """Collect past seasons by (year, week): scoreboard, every game's summary + core-API odds, and the
    season's team dimension. One snapshot dir per season (`backfill-<year>`); re-running skips games
    already on disk and keeps the season's original `snapshot_utc`, so a resumed season loads as one
    point-in-time set. No team pages: the site API only serves the current season's stats/roster."""
    weeks = list(weeks)
    manifests: list[dict[str, Any]] = []
    for year in season_years:
        snapshot = backfill_snapshot(year)
        existing = settings.processed_dir / "espn" / snapshot / "scoreboard.json"
        snapshot_utc = read_json(existing)["meta"]["snapshot_utc"] if existing.exists() and not force else None
        collector = ESPNCollector(settings, snapshot, snapshot_utc=snapshot_utc, core_odds=True)
        log.info("backfill %d weeks %s -> %s (snapshot_utc %s)", year, weeks, collector.out_dir, collector.snapshot_utc)
        async with ESPNClient(settings, raw_dir=collector.raw_dir if keep_raw else None) as client:
            scoreboard = await collector.collect_scoreboard_weeks(client, year, weeks, season_type)
            game_ids = [g["game_id"] for g in scoreboard["games"]]
            games_summary = await collector.collect_games(client, game_ids, skip_existing=not force)
            team_dim = await collector.collect_season_team_list(client, year)
        index = read_json(collector.out_dir / "games" / "_index.json")
        manifest = {
            "meta": _meta(collector.snapshot_utc, collector.snapshot),
            "out_dir": str(collector.out_dir),
            "season_year": year,
            "season_type": season_type,
            "weeks": weeks,
            "scoreboard_games": len(game_ids),
            "games_fetched": len(games_summary),
            "games_skipped_existing": len(index["skipped_existing"]),
            "games_failed": index["failed"],
            "team_dim": len(team_dim["teams"]),
        }
        write_json(collector.out_dir / "manifest.json", manifest)
        if on_season_done is not None:
            on_season_done(collector.out_dir, manifest)
        manifests.append(manifest)
    return manifests
