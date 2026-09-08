"""Live replay: follow in-progress games, rebuilding each replay page as plays arrive, served locally.

`ncaaf-clv replay --live --game-id <id>` (or `--all --db <sqlite>` for every open game around kickoff)
polls ESPN's summary for each game every `interval` seconds into a `live-<tag>` ESPN snapshot (the same
three files a refresh writes, overwritten on every poll), rebuilds the replay payload with
`build.build_replay_data` and writes two files per game to the served directory: `<id>.html`, the
self-contained page as of the last poll, and `<id>.json`, the same payload, which the open page polls to
append new plays without reloading. A small HTTP server on 127.0.0.1 serves that directory (a page
opened from disk cannot fetch a sibling file), with `Cache-Control: no-store` so every poll sees the
latest payload. The loop ends once every game is final; the HTML left on disk is the finished replay.
Nothing is loaded into SQLite: `refresh` does that once the game is final.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import webbrowser
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from functools import partial
from html import escape
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

from ncaaf_clv.common import iso_utc, parse_espn_datetime, read_json, snapshot_tag, utc_now
from ncaaf_clv.config import Settings
from ncaaf_clv.espn.client import BlockedError, ESPNClient
from ncaaf_clv.espn.collect import ESPNCollector
from ncaaf_clv.espn.refresh import open_games
from ncaaf_clv.replay.build import (
    LogoFetcher,
    build_replay_data,
    default_logo_fetcher,
    logo_cache_dir,
    logo_data_uri,
    render_replay,
    replay_out_dir,
)

log = logging.getLogger(__name__)

LIVE_PREFIX = "live-"
DEFAULT_INTERVAL_S = 15.0  # ESPN serves the summary with Cache-Control: max-age=10
PREGAME_INTERVAL_S = 60.0  # a game that has not kicked off is checked once a minute
MIN_INTERVAL_S = 5.0
DEFAULT_PORT = 8765
KICKOFF_LOOKAHEAD = timedelta(minutes=30)  # --all: open games kicking off within this window ...
KICKOFF_LOOKBACK = timedelta(hours=6)  # ... or that kicked off up to this long ago
FINAL_STATE = "post"  # ESPN status.type.state: pre | in | post

PollFn = Callable[[ESPNClient, str], Awaitable[Any]]
SleepFn = Callable[[float], Awaitable[None]]
ClockFn = Callable[[], float]  # monotonic seconds; tests pair a fake clock with a fake sleep

INDEX_TEMPLATE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>Live replays</title>
<style>
  :root{{color-scheme:light dark;--bg:#eef1ec;--surface:#fff;--text:#16211a;--muted:#5d6b61;--line:#cfd7cf;
    --live:#e0322b}}
  @media (prefers-color-scheme: dark){{
    :root{{--bg:#0e1210;--surface:#161c18;--text:#e8ece8;--muted:#98a49c;--line:#2a332d}}
  }}
  body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 'Barlow',system-ui,'Segoe UI',Roboto,sans-serif}}
  main{{max-width:760px;margin:0 auto;padding:28px 16px}}
  h1{{font-size:26px;margin:0 0 4px}}
  .sub{{color:var(--muted);font-size:13px;margin:0 0 18px}}
  table{{width:100%;border-collapse:collapse;background:var(--surface);border:1px solid var(--line);border-radius:10px;
    overflow:hidden}}
  th,td{{padding:10px 12px;text-align:left;border-bottom:1px solid var(--line)}}
  th{{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}}
  td.n{{text-align:right;font-variant-numeric:tabular-nums}}
  a{{color:inherit;font-weight:600}}
  .s{{font-size:12px;letter-spacing:.08em;text-transform:uppercase;font-weight:700}}
  .s.in{{color:var(--live)}}
  .s.post,.s.pre{{color:var(--muted)}}
</style>
<main>
  <h1>Live replays</h1>
  <p class="sub">{n} game(s) · snapshot {snapshot} · last poll {polled} · this list refreshes every 30 s</p>
  <table>
    <tr><th>Game</th><th>State</th><th>Plays</th><th>Polls</th></tr>
{rows}
  </table>
</main>
"""


def live_snapshot_tag(now: datetime | None = None) -> str:
    return f"{LIVE_PREFIX}{snapshot_tag(now or utc_now())}"


def open_games_near_kickoff(conn: sqlite3.Connection, season_year: int, now: datetime | None = None) -> list[str]:
    """Open games (latest `espn_games` row not final) kicking off within KICKOFF_LOOKAHEAD, or that
    kicked off up to KICKOFF_LOOKBACK ago. Games with no kickoff time are skipped."""
    now = now or utc_now()
    keep: list[str] = []
    for g in open_games(conn, season_year):
        kickoff = parse_espn_datetime(g.get("kickoff_utc"))
        if kickoff is not None and now - KICKOFF_LOOKBACK <= kickoff <= now + KICKOFF_LOOKAHEAD:
            keep.append(g["game_id"])
    return keep


@dataclass
class LiveGame:
    game_id: str
    state: str = "pre"
    title: str | None = None
    polls: int = 0  # successful ESPN fetches
    seq: int = 0  # payload version; bumps when the rebuilt payload differs, the page applies it on change
    plays: int = 0
    errors: int = 0
    html: str | None = None
    json: str | None = None
    next_poll: float = field(default=0.0, repr=False, compare=False)  # time.monotonic()
    digest: str | None = field(default=None, repr=False, compare=False)  # sha1 of the last payload

    def summary(self) -> dict[str, Any]:
        out = asdict(self)
        del out["next_poll"], out["digest"]
        return out


# ------------------------------------------------------------------------------- server


class _NoStoreHandler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - base class signature
        log.debug("http %s", format % args)


class LiveServer:
    """Serves the live replay directory on 127.0.0.1 from a daemon thread. `port=0` picks a free port."""

    def __init__(self, directory: Path, port: int = DEFAULT_PORT, host: str = "127.0.0.1") -> None:
        handler = partial(_NoStoreHandler, directory=str(directory))
        self._server = ThreadingHTTPServer((host, port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="ncaaf-clv-live-http", daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


# ------------------------------------------------------------------------------- pages


def _write_atomic(path: Path, text: str) -> None:
    """Write via a sibling temp file + rename so the page never fetches a half-written payload."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def render_index(games: list[LiveGame], snapshot: str, polled_utc: str) -> str:
    labels = {"in": "Live", "post": "Final", "pre": "Pregame"}
    rows = []
    for g in games:
        name = escape(g.title or g.game_id)
        link = f'<a href="{g.game_id}.html">{name}</a>' if g.html else name
        rows.append(
            f'    <tr><td>{link}</td><td class="s {escape(g.state)}">{labels.get(g.state, escape(g.state))}</td>'
            f'<td class="n">{g.plays}</td><td class="n">{g.polls}</td></tr>'
        )
    return INDEX_TEMPLATE.format(
        n=len(games), snapshot=escape(snapshot), polled=escape(polled_utc), rows="\n".join(rows)
    )


async def _poll_game(
    g: LiveGame,
    collector: ESPNCollector,
    client: ESPNClient,
    poll: PollFn,
    out_dir: Path,
    interval: float,
    logo_uris: dict[str, str],
    cache_dir: Path,
    fetcher: LogoFetcher | None,
    logos: bool,
    clock: ClockFn = time.monotonic,
) -> None:
    """One ESPN fetch for `g`: rewrite its snapshot files, rebuild `<id>.json` + `<id>.html`, schedule the
    next poll. Transient failures are counted and retried; a blocking signal propagates."""
    started = clock()
    collector.snapshot_utc = iso_utc(utc_now())  # each poll's files carry their own fetch time
    try:
        await poll(client, g.game_id)
    except BlockedError:
        raise
    except (httpx.HTTPError, OSError, ValueError, KeyError) as exc:
        g.errors += 1
        g.next_poll = started + interval
        log.warning("live %s: poll failed (%s); retrying in %.0fs", g.game_id, exc, interval)
        return
    g.polls += 1
    game_dir = collector.out_dir / "games" / g.game_id
    gc = read_json(game_dir / "gamecast.json")
    game = gc.get("game") or {}
    g.state = game.get("status_state") or "pre"
    if g.state == "pre":
        g.next_poll = started + max(interval, PREGAME_INTERVAL_S)
        log.info("live %s: not started (%s)", g.game_id, game.get("status_detail") or "scheduled")
        return
    if logos:
        for t in gc.get("teams") or []:
            if t["team_id"] not in logo_uris:
                uri = logo_data_uri(t["team_id"], t.get("team_logo"), cache_dir, fetcher)
                if uri:
                    logo_uris[t["team_id"]] = uri
    try:
        data = build_replay_data(game_dir, logo_uris)
    except ValueError as exc:  # kicked off, but ESPN has not written a play yet
        g.next_poll = started + interval
        log.info("live %s: %s", g.game_id, exc)
        return
    digest = hashlib.sha1(json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    if digest != g.digest:  # seq only moves when the content did, so an idle page (halftime) does not rebuild
        g.digest = digest
        g.seq += 1
    data["live"] = {
        "seq": g.seq,
        "state": g.state,
        "polledUtc": collector.snapshot_utc,
        "interval": interval,
        "url": f"{g.game_id}.json",
    }
    json_path, html_path = out_dir / f"{g.game_id}.json", out_dir / f"{g.game_id}.html"
    _write_atomic(json_path, json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    _write_atomic(html_path, render_replay(data))
    added = len(data["plays"]) - g.plays
    g.plays, g.title, g.html, g.json = len(data["plays"]), data["title"], str(html_path), str(json_path)
    last = data["plays"][-1]
    home, away = data["teams"][data["game"]["home"]], data["teams"][data["game"]["away"]]
    log.info(  # ASCII only: the scheduled-task log is written through a cp1252 console
        "live %s: %s | Q%s %s | %s %d-%s %d | %d plays (+%d)%s",
        g.game_id,
        g.title,
        last["per"],
        last["clk"],
        away["abbr"],
        away["score"],
        home["abbr"],
        home["score"],
        g.plays,
        added,
        " | FINAL" if g.state == FINAL_STATE else "",
    )
    g.next_poll = started + interval


async def run_live(
    settings: Settings,
    game_ids: list[str],
    *,
    snapshot: str | None = None,
    out_dir: Path | None = None,
    interval: float = DEFAULT_INTERVAL_S,
    port: int = DEFAULT_PORT,
    serve: bool = True,
    logos: bool = True,
    fetcher: LogoFetcher | None = None,
    open_browser: bool = False,
    max_rounds: int | None = None,
    poll: PollFn | None = None,
    sleep: SleepFn = asyncio.sleep,
    clock: ClockFn = time.monotonic,
) -> dict[str, Any]:
    """Follow `game_ids` until every game is final (or `max_rounds` polling rounds have run). Writes the
    ESPN snapshot `snapshot` (default `live-<now>`) and the pages under `out_dir` (default
    `data/processed/replays/<snapshot>`); serves `out_dir` on `port` unless `serve=False`. `poll`, `sleep`
    and `clock` are injection points for tests (default: `ESPNCollector.collect_game`, `asyncio.sleep`,
    `time.monotonic`)."""
    if not game_ids:
        raise ValueError("live: no games to follow")
    interval = max(MIN_INTERVAL_S, float(interval))
    collector = ESPNCollector(settings, snapshot or live_snapshot_tag(), core_odds=False)
    out_dir = out_dir or replay_out_dir(settings, collector.snapshot)
    out_dir.mkdir(parents=True, exist_ok=True)
    if logos and fetcher is None:
        fetcher = default_logo_fetcher(settings)
    cache_dir = logo_cache_dir(settings)
    logo_uris: dict[str, str] = {}
    games = [LiveGame(gid) for gid in dict.fromkeys(game_ids)]
    poll = poll or collector.collect_game
    server = LiveServer(out_dir, port) if serve else None
    if server:
        server.start()
        log.info("live: serving %s at %s (index.html lists the games)", out_dir, server.url)
    opened: set[str] = set()
    rounds = 0
    try:
        async with ESPNClient(settings, raw_dir=collector.raw_dir) as client:
            while True:
                now = clock()
                due = [g for g in games if g.state != FINAL_STATE and g.next_poll <= now]
                if due:
                    rounds += 1
                    await asyncio.gather(
                        *(
                            _poll_game(
                                g,
                                collector,
                                client,
                                poll,
                                out_dir,
                                interval,
                                logo_uris,
                                cache_dir,
                                fetcher,
                                logos,
                                clock,
                            )
                            for g in due
                        )
                    )
                    _write_atomic(
                        out_dir / "index.html", render_index(games, collector.snapshot, collector.snapshot_utc)
                    )
                    if open_browser and server:
                        for g in due:
                            if g.html and g.game_id not in opened:
                                opened.add(g.game_id)
                                webbrowser.open(f"{server.url}{g.game_id}.html")
                pending = [g for g in games if g.state != FINAL_STATE]
                if not pending:
                    log.info("live: every game is final")
                    if server:  # let an open page fetch the final payload before the server goes away
                        await sleep(interval + MIN_INTERVAL_S)
                    break
                if max_rounds is not None and rounds >= max_rounds:
                    break
                await sleep(max(0.0, min(g.next_poll for g in pending) - clock()))
    finally:
        if server:
            server.stop()
    return {
        "snapshot": collector.snapshot,
        "espn_dir": str(collector.out_dir),
        "out_dir": str(out_dir),
        "url": server.url if server else None,
        "interval": interval,
        "rounds": rounds,
        "games": [g.summary() for g in games],
    }
