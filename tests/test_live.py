"""Live replay: polling loop, page + payload writing, server headers, open-set filter."""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen

import httpx
import pytest

from ncaaf_clv import storage
from ncaaf_clv.common import read_json, write_json
from ncaaf_clv.config import Settings
from ncaaf_clv.replay.live import (
    LIVE_PREFIX,
    PREGAME_INTERVAL_S,
    LiveGame,
    LiveServer,
    live_snapshot_tag,
    open_games_near_kickoff,
    render_index,
    run_live,
)
from test_refresh import _game
from test_replay import game_dir  # noqa: F401 - fixture

GAME = "401"


class FakeTime:
    """Monotonic clock that only advances when the loop sleeps, so polls come due instantly."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def _fake_poll(src: Path, dest_root: Path, script: list[tuple[str, int] | Exception]):
    """A `poll(client, game_id)` stand-in: each call takes the next script entry, either an exception to
    raise or `(status_state, n_plays)` to write into the live snapshot's game folder from the fixture."""
    calls: list[str] = []

    async def poll(_client, game_id: str) -> None:
        step = script[min(len(calls), len(script) - 1)]
        calls.append(game_id)
        if isinstance(step, Exception):
            raise step
        state, n = step
        gc = read_json(src / "gamecast.json")
        gc["game"]["status_state"] = state
        gc["game"]["status_detail"] = {"pre": "7:30 PM ET", "in": "2:00 - 2nd", "post": "Final"}[state]
        pbp = read_json(src / "play_by_play.json")
        pbp["plays"] = pbp["plays"][:n]
        write_json(dest_root / "games" / game_id / "gamecast.json", gc)
        write_json(dest_root / "games" / game_id / "play_by_play.json", pbp)

    return poll, calls


def _settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data")


def test_run_live_polls_until_final_and_writes_pages(game_dir: Path, tmp_path: Path) -> None:  # noqa: F811
    settings = _settings(tmp_path)
    snapshot = "live-test"
    poll, calls = _fake_poll(game_dir, settings.processed_dir / "espn" / snapshot, [("in", 3), ("in", 5), ("post", 6)])
    ft = FakeTime()
    manifest = asyncio.run(
        run_live(
            settings, [GAME], snapshot=snapshot, poll=poll, sleep=ft.sleep, clock=ft.clock, serve=False, logos=False
        )
    )
    assert calls == [GAME, GAME, GAME]  # stops by itself once the game is final
    assert ft.sleeps == [15.0, 15.0]  # one interval between rounds; no grace sleep without a server
    g = manifest["games"][0]
    assert (g["state"], g["polls"], g["seq"], g["plays"], g["errors"]) == ("post", 3, 3, 6, 0)
    assert manifest["rounds"] == 3 and manifest["url"] is None
    out = Path(manifest["out_dir"])
    assert out == settings.processed_dir / "replays" / snapshot
    payload = json.loads((out / f"{GAME}.json").read_text(encoding="utf-8"))
    assert payload["live"] == {
        "seq": 3,
        "state": "post",
        "polledUtc": payload["live"]["polledUtc"],
        "interval": 15.0,
        "url": f"{GAME}.json",
    }
    assert [p["id"] for p in payload["plays"]] == ["p1", "p3", "p2", "p4", "p5", "p6"]  # clock-ordered
    html = (out / f"{GAME}.html").read_text(encoding="utf-8")
    assert '"live":{"seq":3' in html and "<title>North Alabama at Arkansas Replay</title>" in html
    index = (out / "index.html").read_text(encoding="utf-8")
    assert f'<a href="{GAME}.html">North Alabama at Arkansas</a>' in index and "Final" in index
    assert not list(out.glob("*.tmp"))  # atomic writes leave nothing behind


def test_run_live_pregame_backs_off_and_writes_nothing(game_dir: Path, tmp_path: Path) -> None:  # noqa: F811
    settings = _settings(tmp_path)
    poll, calls = _fake_poll(game_dir, settings.processed_dir / "espn" / "live-pre", [("pre", 0), ("in", 3)])
    ft = FakeTime()
    manifest = asyncio.run(
        run_live(
            settings,
            [GAME],
            snapshot="live-pre",
            poll=poll,
            sleep=ft.sleep,
            clock=ft.clock,
            serve=False,
            logos=False,
            max_rounds=2,
        )
    )
    g = manifest["games"][0]
    assert calls == [GAME, GAME] and (g["state"], g["polls"], g["seq"], g["plays"]) == ("in", 2, 1, 3)
    assert ft.sleeps == [PREGAME_INTERVAL_S]  # a scheduled game is re-checked once a minute, not every 15 s
    assert "Live" in (Path(manifest["out_dir"]) / "index.html").read_text(encoding="utf-8")


def test_run_live_counts_transient_errors_and_recovers(game_dir: Path, tmp_path: Path) -> None:  # noqa: F811
    settings = _settings(tmp_path)
    poll, calls = _fake_poll(
        game_dir,
        settings.processed_dir / "espn" / "live-err",
        [httpx.ConnectError("boom"), ("in", 4), ("post", 6)],
    )
    ft = FakeTime()
    manifest = asyncio.run(
        run_live(
            settings, [GAME], snapshot="live-err", poll=poll, sleep=ft.sleep, clock=ft.clock, serve=False, logos=False
        )
    )
    g = manifest["games"][0]
    assert len(calls) == 3 and (g["errors"], g["polls"], g["seq"], g["state"]) == (1, 2, 2, "post")
    assert ft.sleeps == [15.0, 15.0]  # the failed poll is retried after a normal interval


def test_run_live_dedupes_games_and_rejects_empty(game_dir: Path, tmp_path: Path) -> None:  # noqa: F811
    settings = _settings(tmp_path)
    poll, calls = _fake_poll(game_dir, settings.processed_dir / "espn" / "live-dup", [("post", 6)])
    ft = FakeTime()
    manifest = asyncio.run(
        run_live(
            settings,
            [GAME, GAME],
            snapshot="live-dup",
            poll=poll,
            sleep=ft.sleep,
            clock=ft.clock,
            serve=False,
            logos=False,
        )
    )
    assert calls == [GAME] and len(manifest["games"]) == 1
    try:
        asyncio.run(run_live(settings, [], serve=False))
    except ValueError as exc:
        assert "no games" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("empty game list must be rejected")


def test_live_server_serves_directory_with_no_store(tmp_path: Path) -> None:
    (tmp_path / "x.json").write_text('{"ok": true}', encoding="utf-8")
    server = LiveServer(tmp_path, port=0)
    server.start()
    try:
        with urlopen(server.url + "x.json?t=1") as resp:  # noqa: S310 - loopback
            assert resp.headers["Cache-Control"] == "no-store"
            assert json.loads(resp.read()) == {"ok": True}
    finally:
        server.stop()
    assert server.url.startswith("http://127.0.0.1:")


def test_run_live_serves_the_pages_while_polling(
    game_dir: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the server up, the payload is fetchable over HTTP between rounds and during the final grace."""
    settings = _settings(tmp_path)
    poll, _ = _fake_poll(game_dir, settings.processed_dir / "espn" / "live-srv", [("in", 3), ("post", 6)])
    server_url: list[str] = []
    orig_start = LiveServer.start
    monkeypatch.setattr(LiveServer, "start", lambda self: (orig_start(self), server_url.append(self.url)))
    ft = FakeTime()
    probed: list[int] = []

    async def probe(delay: float) -> None:  # stands in for asyncio.sleep, so it runs while serving
        with urlopen(server_url[0] + f"{GAME}.json") as resp:  # noqa: S310 - loopback
            probed.append(json.load(resp)["live"]["seq"])
        await ft.sleep(delay)

    manifest = asyncio.run(
        run_live(settings, [GAME], snapshot="live-srv", poll=poll, sleep=probe, clock=ft.clock, port=0, logos=False)
    )
    assert manifest["url"] == server_url[0] and manifest["url"].startswith("http://127.0.0.1:")
    assert probed == [1, 2]  # seq 1 after round one, seq 2 (final) during the grace sleep
    assert ft.sleeps == [15.0, 20.0]  # interval, then interval + 5 s grace before the server stops


def test_open_games_near_kickoff_window(tmp_path: Path) -> None:
    conn = storage.connect(tmp_path / "t.db")
    storage.insert_rows(
        conn,
        "espn_games",
        [
            _game("1", "2026-09-06T16:00:00Z", "pre", kickoff="2026-09-06T23:30:00Z"),  # kicks off in 20 min
            _game("2", "2026-09-06T16:00:00Z", "in", "STATUS_IN_PROGRESS", kickoff="2026-09-06T19:30:00Z"),
            _game("3", "2026-09-06T16:00:00Z", "pre", kickoff="2026-09-07T23:30:00Z"),  # tomorrow
            _game("4", "2026-09-06T16:00:00Z", "in", "STATUS_IN_PROGRESS", kickoff="2026-09-06T15:00:00Z"),  # > 6 h
            _game("5", "2026-09-06T16:00:00Z", "post", "STATUS_FINAL", kickoff="2026-09-06T22:00:00Z"),  # closed
            _game("6", "2026-09-06T16:00:00Z", "pre", kickoff=None),  # no kickoff time
        ],
    )
    now = datetime(2026, 9, 6, 23, 10, tzinfo=UTC)
    assert open_games_near_kickoff(conn, 2026, now) == ["2", "1"]  # open_games orders by kickoff
    conn.close()


def test_live_snapshot_tag_and_index() -> None:
    assert live_snapshot_tag(datetime(2026, 9, 7, 23, 30, tzinfo=UTC)) == f"{LIVE_PREFIX}20260907T233000Z"
    games = [LiveGame("1", state="in", title="A at B", plays=12, polls=3, html="1.html"), LiveGame("2")]
    html = render_index(games, "live-x", "2026-09-07T23:30:00Z")
    assert '<a href="1.html">A at B</a>' in html and 'class="s in">Live<' in html
    assert "<td>2</td>" in html and "Pregame" in html  # no link until the game has a page
    assert games[0].summary()["plays"] == 12 and "next_poll" not in games[0].summary()


def test_fixture_copy_is_independent(game_dir: Path, tmp_path: Path) -> None:  # noqa: F811
    """The fake poll rewrites files into the live snapshot, never the fixture's own folder."""
    before = (game_dir / "play_by_play.json").read_bytes()
    dest = tmp_path / "elsewhere"
    shutil.copytree(game_dir, dest / "games" / GAME)
    poll, _ = _fake_poll(game_dir, dest, [("in", 2)])
    asyncio.run(poll(None, GAME))
    assert (game_dir / "play_by_play.json").read_bytes() == before
    assert len(read_json(dest / "games" / GAME / "play_by_play.json")["plays"]) == 2
