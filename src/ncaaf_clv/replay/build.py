"""Play-by-play replay: one self-contained HTML page per game from an ESPN snapshot's game folder.

The page (`template.html`) animates every play of a collected game on a field drawn in the two teams'
colours, with a broadcast-style scoreboard (score, quarter, clock, down and distance, possession), the
drive chart, ESPN's win probability and a drive log. Everything the page needs is inlined: the play
data, the team logos (data URIs) and the CSS/JS, so the file can be opened from disk, shared or
published as-is.

Data preparation happens here so the JavaScript stays a renderer:

* plays are ordered by (period, clock) — ESPN's feed lists a timeout after the play it preceded;
* the ball spot after a play is taken from the next scrimmage play's start (ESPN writes `end_yard_line`
  0 on some penalties and turnovers), touchdowns end in the end zone, field goals at the posts;
* timeouts and quarter ends carry the previous play's spot, possession and down;
* ESPN's win probability is keyed by play id and carried forward over the plays it skips.

Yard lines follow ESPN's absolute convention: 0 = home goal line, 100 = away goal line.
"""

from __future__ import annotations

import base64
import html
import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from ncaaf_clv.common import parse_espn_datetime, read_json
from ncaaf_clv.config import Settings

log = logging.getLogger(__name__)

TEMPLATE_PATH = Path(__file__).with_name("template.html")
DATA_TOKEN = "__DATA__"
TITLE_TOKEN = "__TITLE__"
HOLD_TYPES = frozenset({"Timeout", "End Period", "End of Game"})
DISPLAY_TZ = ZoneInfo("America/New_York")  # kickoff shown in ET, the pipeline's --date convention
SNAP_CLOCK_RE = re.compile(r"^\((\d{1,2}:\d{2})\)\s*")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

LogoFetcher = Callable[[str], bytes | None]


# ------------------------------------------------------------------------------- paths


def espn_snapshot_dir(settings: Settings, espn_snapshot: str) -> Path:
    return settings.processed_dir / "espn" / espn_snapshot


def replay_out_dir(settings: Settings, espn_snapshot: str) -> Path:
    return settings.processed_dir / "replays" / espn_snapshot


def logo_cache_dir(settings: Settings) -> Path:
    return settings.raw_espn_dir / "logos"


def default_snapshot(settings: Settings) -> str | None:
    """Most recently written ESPN snapshot that holds game folders (weekly, refresh or backfill)."""
    root = settings.processed_dir / "espn"
    if not root.is_dir():
        return None
    candidates = [p for p in root.iterdir() if p.is_dir() and (p / "games").is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime).name


def list_game_ids(snapshot_dir: Path) -> list[str]:
    """Game folders in the snapshot that have a play-by-play file."""
    games = snapshot_dir / "games"
    if not games.is_dir():
        return []
    return sorted(p.name for p in games.iterdir() if (p / "play_by_play.json").is_file())


# ------------------------------------------------------------------------------- helpers


def clock_seconds(clock: str | None) -> int:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", (clock or "").strip())
    return int(m.group(1)) * 60 + int(m.group(2)) if m else 0


def _num(value: Any, default: int) -> int:
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else default


def _hex(value: str | None) -> str | None:
    if not value:
        return None
    v = str(value).strip().lstrip("#").lower()
    return v if re.fullmatch(r"[0-9a-f]{6}", v) else None


def badge_data_uri(abbr: str, color: str) -> str:
    """Fallback 'logo': the abbreviation on a team-coloured disc (used when the logo is not fetched)."""
    size = 34 if len(abbr) <= 3 else 26
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        f'<circle cx="50" cy="50" r="48" fill="#{color}" stroke="#fff" stroke-width="4"/>'
        f'<text x="50" y="50" fill="#fff" font-family="Arial Narrow,Arial,sans-serif" font-weight="700" '
        f'font-size="{size}" text-anchor="middle" dominant-baseline="central">{html.escape(abbr)}</text></svg>'
    )
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


def image_data_uri(data: bytes) -> str:
    if data.startswith(PNG_MAGIC):
        mime = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif b"<svg" in data[:512]:
        mime = "image/svg+xml"
    else:
        mime = "image/png"
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def default_logo_fetcher(settings: Settings) -> LogoFetcher:
    """HTTP GET for team logos (a.espncdn.com). Same user-agent rule as the JSON client."""
    headers = {"User-Agent": settings.espn_user_agent} if settings.espn_user_agent else {}

    def fetch(url: str) -> bytes | None:
        try:
            resp = httpx.get(url, headers=headers, timeout=settings.http_timeout_s, follow_redirects=True)
        except httpx.HTTPError as exc:
            log.warning("logo fetch failed for %s: %s", url, exc)
            return None
        if resp.status_code != 200 or not resp.content:
            log.warning("logo fetch for %s returned HTTP %d", url, resp.status_code)
            return None
        return resp.content

    return fetch


def logo_data_uri(team_id: str, url: str | None, cache_dir: Path, fetcher: LogoFetcher | None) -> str | None:
    """Cached logo as a data URI: `<cache_dir>/<team_id>.png` is reused; else fetched (when a fetcher is
    given) and cached. None when unavailable."""
    cached = cache_dir / f"{team_id}.png"
    if cached.is_file():
        return image_data_uri(cached.read_bytes())
    if not url or fetcher is None:
        return None
    data = fetcher(url)
    if not data:
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(data)
    return image_data_uri(data)


# ------------------------------------------------------------------------------- data


def _team(t: dict[str, Any], logo_uri: str | None) -> dict[str, Any]:
    abbr = t.get("team_abbreviation") or "TM"
    color = _hex(t.get("team_color")) or "444444"
    return {
        "id": t.get("team_id"),
        "abbr": abbr,
        "name": t.get("team_display_name") or abbr,
        "location": t.get("team_location") or abbr,
        "color": color,
        "alt": _hex(t.get("team_alternate_color")),
        "logo": logo_uri or badge_data_uri(abbr, color),
        "line": [_num(x, 0) for x in (t.get("linescores") or [])],
        "score": _num(t.get("score"), 0),
    }


def _end_position(plays: list[dict[str, Any]], idx: int, home_id: str) -> int:
    """Ball spot after `plays[idx]` (absolute yard line). Touchdowns end in the end zone, field goals at
    the posts; otherwise the next scrimmage play's start is authoritative (skipping timeouts and period
    ends) because ESPN's `end_yard_line` is unreliable on penalties and turnovers."""
    p = plays[idx]
    play_type = p.get("play_type") or ""
    off = p.get("offense_team_id")
    end = _num(p.get("end_yard_line"), _num(p.get("start_yard_line"), 50))
    if p.get("scoring_play") and "Touchdown" in play_type:
        return end if end in (0, 100) else (100 if off == home_id else 0)
    if "Field Goal" in play_type:
        return 100 if off == home_id else 0
    for nxt in plays[idx + 1 :]:
        if (nxt.get("play_type") or "") in HOLD_TYPES:
            continue
        if nxt.get("play_type") == "Kickoff":
            return end
        return _num(nxt.get("start_yard_line"), end)
    return end


def _kickoff_label(kickoff_utc: str | None) -> str:
    dt = parse_espn_datetime(kickoff_utc)
    if dt is None:
        return ""
    local = dt.astimezone(DISPLAY_TZ)
    return f"{local:%a}, {local:%b} {local.day}, {local.year} · {local.hour % 12 or 12}:{local:%M} {local:%p} ET"


def _note(pbp: dict[str, Any], gc: dict[str, Any], teams: dict[str, dict[str, Any]], home: str, away: str) -> str:
    game = gc.get("game") or {}
    snapshot = (pbp.get("meta") or {}).get("snapshot") or "?"
    parts = [
        f"Source: ESPN play-by-play, snapshot {html.escape(str(snapshot))}. Clock and score are replayed from each "
        "play's recorded clock, period and running score; the pre-snap clock is parsed from the play text where "
        "ESPN records it."
    ]
    odds = (gc.get("espn_odds") or [None])[0]
    if odds:
        spread = odds.get("close_home_spread")
        spread = odds.get("home_spread") if spread is None else spread
        total = odds.get("close_total")
        total = odds.get("total") if total is None else total
        if spread is not None:
            line = f"{teams[home]['abbr']} {spread:+g}"
            if total is not None:
                line += f", total {total:g}"
            provider = html.escape(str(odds.get("provider_name") or "ESPN"))
            parts.append(f"<b>Closing line ({provider}):</b> {line}.")
            hs, as_ = _num(game.get("home_score"), 0), _num(game.get("away_score"), 0)
            if game.get("status_state") == "post":
                winner = teams[home]["abbr"] if hs >= as_ else teams[away]["abbr"]
                parts.append(f"Final margin {winner} by {abs(hs - as_)}.")
    for tid in (away, home):
        if teams[tid]["alt"] is None:
            parts.append(
                f"End zone colours are ESPN's listed team colours ({teams[tid]['abbr']} lists #{teams[tid]['color']} "
                "with no alternate)."
            )
    return " ".join(parts)


def build_replay_data(game_dir: Path, logo_uris: dict[str, str] | None = None) -> dict[str, Any]:
    """Everything the page needs, from `<game_dir>/play_by_play.json` + `gamecast.json`.

    `logo_uris` maps team_id -> data URI; teams without one get a coloured abbreviation badge."""
    pbp = read_json(game_dir / "play_by_play.json")
    gc = read_json(game_dir / "gamecast.json")
    game = gc.get("game") or {}
    home, away = game.get("home_team_id"), game.get("away_team_id")
    if not home or not away:
        raise ValueError(f"{game_dir}: gamecast.json has no home/away team ids")
    raw_plays = pbp.get("plays") or []
    if not raw_plays:
        raise ValueError(f"{game_dir}: no plays in play_by_play.json")
    logo_uris = logo_uris or {}
    teams = {t["team_id"]: _team(t, logo_uris.get(t["team_id"])) for t in gc.get("teams") or []}
    for tid in (home, away):
        teams.setdefault(tid, _team({"team_id": tid}, logo_uris.get(tid)))

    order = sorted(
        range(len(raw_plays)),
        key=lambda i: (_num(raw_plays[i].get("period"), 0), -clock_seconds(raw_plays[i].get("clock")), i),
    )
    plays = [raw_plays[i] for i in order]
    wp_by_play = {w["play_id"]: w["home_win_pct"] for w in gc.get("win_probability") or [] if w.get("play_id")}
    win_prob = gc.get("win_probability") or []
    pregame = win_prob[0]["home_win_pct"] if win_prob else 0.5
    drive_order = {d["drive_id"]: d["drive_order"] for d in pbp.get("drives") or []}

    out: list[dict[str, Any]] = []
    prev: dict[str, Any] | None = None
    current_wp = pregame
    for idx, p in enumerate(plays):
        txt = p.get("text") or ""
        m = SNAP_CLOCK_RE.match(txt)
        snap = m.group(1) if m else None
        if m:
            txt = txt[m.end() :]
        off = p.get("offense_team_id") or (prev["off"] if prev else home)
        q: dict[str, Any] = {
            "id": p.get("play_id"),
            "per": _num(p.get("period"), 1),
            "clk": p.get("clock") or "0:00",
            "snap": snap,
            "off": off,
            "type": p.get("play_type") or "Play",
            "txt": txt,
            "as": _num(p.get("away_score"), prev["as"] if prev else 0),
            "hs": _num(p.get("home_score"), prev["hs"] if prev else 0),
            "sc": bool(p.get("scoring_play")),
            "pen": bool(p.get("is_penalty")),
            "to": bool(p.get("is_turnover")),
            "dd": p.get("down_distance_text"),
            "drv": drive_order.get(p.get("drive_id")),
        }
        if q["type"] in HOLD_TYPES and prev is not None:
            q.update(
                s=prev["e"],
                e=prev["e"],
                pos0=prev["pos1"],
                pos1=prev["pos1"],
                d0=prev["d1"],
                dist0=prev["dist1"],
                d1=prev["d1"],
                dist1=prev["dist1"],
                dd=None,
            )
        else:
            q.update(
                s=_num(p.get("start_yard_line"), prev["e"] if prev else 50),
                e=_end_position(plays, idx, home),
                pos0=p.get("start_possession_team_id") or off,
                pos1=p.get("end_possession_team_id") or off,
                d0=_num(p.get("start_down"), 0),
                dist0=_num(p.get("start_distance"), 0),
                d1=_num(p.get("end_down"), 0),
                dist1=_num(p.get("end_distance"), 0),
            )
        if q["id"] in wp_by_play:
            current_wp = wp_by_play[q["id"]]
        q["wp"] = current_wp
        out.append(q)
        prev = q

    drives = [
        {
            "n": d["drive_order"],
            "team": d.get("team_id"),
            "startPer": _num(d.get("start_period"), 1),
            "startClk": d.get("start_clock") or "15:00",
            "start": _num(d.get("start_yard_line"), 50),
            "end": _num(d.get("end_yard_line"), 50),
            "result": d.get("display_result") or d.get("result") or "",
            "desc": d.get("description") or "",
        }
        for d in pbp.get("drives") or []
    ]
    scoring = [
        {
            "id": s.get("play_id"),
            "team": s.get("team_id"),
            "per": _num(s.get("period"), 1),
            "clk": s.get("clock") or "",
            "text": s.get("text") or "",
            "as": _num(s.get("away_score"), 0),
            "hs": _num(s.get("home_score"), 0),
        }
        for s in gc.get("scoring_plays") or []
    ]
    joiner = "vs" if game.get("neutral_site") else "at"
    return {
        "title": f"{teams[away]['location']} {joiner} {teams[home]['location']}",
        "teams": teams,
        "plays": out,
        "drives": drives,
        "scoring": scoring,
        "pregameWp": pregame,
        "note": _note(pbp, gc, teams, home, away),
        "game": {
            "home": home,
            "away": away,
            "week": game.get("week"),
            "date": _kickoff_label(game.get("kickoff_utc")),
            "venue": game.get("venue_name"),
            "city": game.get("venue_city"),
            "state": game.get("venue_state"),
            "attendance": game.get("attendance"),
            "status": game.get("status_detail"),
        },
    }


# ------------------------------------------------------------------------------- render


def render_replay(data: dict[str, Any], template_path: Path = TEMPLATE_PATH) -> str:
    template = template_path.read_text(encoding="utf-8")
    if DATA_TOKEN not in template:
        raise ValueError(f"{template_path}: missing {DATA_TOKEN} placeholder")
    # `</` inside the JSON would end the <script> element early; the escape is invisible to JSON.parse.
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    title = html.escape(f"{data['title']} Replay")
    return template.replace(TITLE_TOKEN, title).replace(DATA_TOKEN, payload)


def run_replay(
    settings: Settings,
    espn_snapshot: str,
    game_ids: list[str],
    out_dir: Path | None = None,
    logos: bool = True,
    fetcher: LogoFetcher | None = None,
) -> dict[str, Any]:
    """Write `<out_dir>/<game_id>.html` for each game; returns a manifest of written and skipped games."""
    snapshot_dir = espn_snapshot_dir(settings, espn_snapshot)
    out_dir = out_dir or replay_out_dir(settings, espn_snapshot)
    if logos and fetcher is None:
        fetcher = default_logo_fetcher(settings)
    cache_dir = logo_cache_dir(settings)
    manifest: dict[str, Any] = {"espn_snapshot": espn_snapshot, "out_dir": str(out_dir), "written": [], "skipped": []}
    for game_id in game_ids:
        game_dir = snapshot_dir / "games" / game_id
        try:
            gc = read_json(game_dir / "gamecast.json")
            logo_uris: dict[str, str] = {}
            for t in gc.get("teams") or [] if logos else []:
                uri = logo_data_uri(t["team_id"], t.get("team_logo"), cache_dir, fetcher)
                if uri:
                    logo_uris[t["team_id"]] = uri
            data = build_replay_data(game_dir, logo_uris)
        except (OSError, ValueError, KeyError) as exc:
            log.warning("replay %s skipped: %s", game_id, exc)
            manifest["skipped"].append({"game_id": game_id, "reason": str(exc)})
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{game_id}.html"
        path.write_text(render_replay(data), encoding="utf-8")
        manifest["written"].append(
            {"game_id": game_id, "title": data["title"], "plays": len(data["plays"]), "path": str(path)}
        )
        log.info("replay %s: %s (%d plays) -> %s", game_id, data["title"], len(data["plays"]), path)
    return manifest
