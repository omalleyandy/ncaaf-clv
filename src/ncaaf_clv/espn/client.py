"""Async ESPN HTTP client: rate-limited, retrying, with raw-response caching.

ESPN's public JSON endpoints back the Gamecast / Box Score / Play-by-Play tabs and the
team Statistics / Schedule / Roster pages. Fetching the JSON directly is equivalent to
clicking those tabs, but deterministic and far cheaper than DOM scraping.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any

import httpx

from ncaaf_clv.common import read_json, write_json
from ncaaf_clv.config import (
    ESPN_CORE_API,
    ESPN_SITE_API,
    REGULAR_SEASON_TYPE,
    Settings,
)

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
BLOCK_STATUS = {403, 429}


class BlockedError(RuntimeError):
    """Raised when ESPN signals blocking (403/429) after retries are exhausted."""


class RateLimiter:
    """Simple token-bucket limiter shared by all concurrent requests."""

    def __init__(self, rate_per_s: float) -> None:
        self._interval = 1.0 / rate_per_s if rate_per_s > 0 else 0.0
        self._next_ok = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        if self._interval == 0:
            return
        async with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_ok - now)
            self._next_ok = max(now, self._next_ok) + self._interval
        if delay:
            await asyncio.sleep(delay)


class ESPNClient:
    """Thin async client. Use as `async with ESPNClient(settings) as c:`."""

    def __init__(self, settings: Settings, raw_dir: Path | None = None) -> None:
        self._settings = settings
        self._raw_dir = raw_dir
        self._limiter = RateLimiter(settings.rate_limit_per_s)
        self._sem = asyncio.Semaphore(settings.max_concurrency)
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> ESPNClient:
        headers = {"Accept": "application/json"}
        if self._settings.espn_user_agent:  # else httpx's default; ESPN's edge 403s bare product tokens
            headers["User-Agent"] = self._settings.espn_user_agent
        self._client = httpx.AsyncClient(timeout=self._settings.http_timeout_s, headers=headers, follow_redirects=True)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ core

    async def get_json(
        self, url: str, params: dict[str, Any] | None = None, cache_key: str | None = None
    ) -> dict[str, Any]:
        """GET JSON with retry/backoff. If `cache_key` and a raw dir are set, the raw
        response is persisted to `<raw_dir>/<cache_key>.json` for audit/replay."""
        assert self._client is not None, "use `async with ESPNClient(...)`"
        attempt = 0
        while True:
            attempt += 1
            async with self._sem:
                await self._limiter.wait()
                try:
                    resp = await self._client.get(url, params=params)
                except httpx.HTTPError as exc:
                    if attempt > self._settings.max_retries:
                        raise
                    backoff = 2**attempt
                    log.warning("HTTP error %s on %s (attempt %d); retry in %ss", exc, url, attempt, backoff)
                    await asyncio.sleep(backoff)
                    continue

            if resp.status_code == 200:
                payload = resp.json()
                if cache_key and self._raw_dir is not None:
                    write_json(self._raw_dir / f"{cache_key}.json", payload)
                return payload

            if resp.status_code in RETRYABLE_STATUS and attempt <= self._settings.max_retries:
                backoff = 2**attempt
                log.warning("HTTP %d on %s (attempt %d); retry in %ss", resp.status_code, url, attempt, backoff)
                await asyncio.sleep(backoff)
                continue

            if resp.status_code in BLOCK_STATUS:
                log.error("Blocking signal HTTP %d from %s", resp.status_code, url)
                raise BlockedError(f"HTTP {resp.status_code} from {url}")

            resp.raise_for_status()
            raise RuntimeError(f"Unexpected HTTP {resp.status_code} from {url}")

    def cached(self, cache_key: str) -> dict[str, Any] | None:
        """Return a previously persisted raw payload, if present."""
        if self._raw_dir is None:
            return None
        path = self._raw_dir / f"{cache_key}.json"
        return read_json(path) if path.exists() else None

    # ------------------------------------------------------------- endpoints

    async def scoreboard(self, date_yyyymmdd: str | None = None, group_id: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"groups": group_id or self._settings.espn_group_id, "limit": 400}
        if date_yyyymmdd:
            params["dates"] = date_yyyymmdd
        key = f"scoreboard/scoreboard_{date_yyyymmdd or 'current'}"
        return await self.get_json(f"{ESPN_SITE_API}/scoreboard", params, cache_key=key)

    async def scoreboard_week(
        self, season_year: int, week: int, season_type: int = REGULAR_SEASON_TYPE, group_id: int | None = None
    ) -> dict[str, Any]:
        """Scoreboard for one (season, week); backs `/scoreboard/_/week/W/year/Y/seasontype/T/group/G`."""
        params: dict[str, Any] = {
            "groups": group_id or self._settings.espn_group_id,
            "limit": 400,
            "dates": season_year,
            "seasontype": season_type,
            "week": week,
        }
        key = f"scoreboard/scoreboard_{season_year}_type{season_type}_week{week:02d}"
        return await self.get_json(f"{ESPN_SITE_API}/scoreboard", params, cache_key=key)

    async def summary(self, event_id: str) -> dict[str, Any]:
        """Backs Gamecast + Box Score + Play-by-Play tabs."""
        return await self.get_json(f"{ESPN_SITE_API}/summary", {"event": event_id}, cache_key=f"summary/{event_id}")

    async def event_odds(self, event_id: str) -> list[dict[str, Any]]:
        """Core API: every sportsbook line ESPN recorded for the game, one item per provider.
        Retained for past seasons (open/close blocks from ESPN BET 2023+, most books 2024+)."""
        url = f"{ESPN_CORE_API}/events/{event_id}/competitions/{event_id}/odds"
        payload = await self.get_json(url, {"limit": 50}, cache_key=f"odds/{event_id}")
        return payload.get("items", [])

    async def group_team_refs(self, season_year: int, group_id: int | None = None) -> list[str]:
        """Core API: team `$ref` URLs for a group (80 = FBS)."""
        gid = group_id or self._settings.espn_group_id
        url = f"{ESPN_CORE_API}/seasons/{season_year}/types/{REGULAR_SEASON_TYPE}/groups/{gid}/teams"
        payload = await self.get_json(url, {"limit": 500}, cache_key=f"teams/group_{gid}_refs")
        return [item["$ref"] for item in payload.get("items", [])]

    async def group_children(self, season_year: int, group_id: int | None = None) -> list[dict[str, Any]]:
        """Core API: conference sub-groups of a group, resolved to {id, name, abbreviation}."""
        gid = group_id or self._settings.espn_group_id
        url = f"{ESPN_CORE_API}/seasons/{season_year}/types/{REGULAR_SEASON_TYPE}/groups/{gid}/children"
        payload = await self.get_json(url, {"limit": 100}, cache_key=f"teams/group_{gid}_children")
        refs = [item["$ref"] for item in payload.get("items", [])]
        groups = await asyncio.gather(*(self.get_json(ref.replace("http://", "https://")) for ref in refs))
        return [
            {
                "group_id": str(g.get("id")),
                "name": g.get("name"),
                "abbreviation": g.get("abbreviation"),
                "parent_group_id": str(gid),
            }
            for g in groups
        ]

    async def season_team(self, season_year: int, team_id: str) -> dict[str, Any]:
        """Core API: the team as it was in `season_year` (conference group of that season, all-star flag)."""
        url = f"{ESPN_CORE_API}/seasons/{season_year}/teams/{team_id}"
        return await self.get_json(url, {"lang": "en"}, cache_key=f"teams/{team_id}/season_team")

    async def group_by_ref(self, ref: str) -> dict[str, Any]:
        """Core API group object (conference or division) from its `$ref`; carries `parent.$ref`."""
        gid = group_id_from_ref(ref)
        return await self.get_json(ref.replace("http://", "https://"), cache_key=f"groups/{gid}")

    async def team_detail(self, team_id: str) -> dict[str, Any]:
        return await self.get_json(f"{ESPN_SITE_API}/teams/{team_id}", cache_key=f"teams/{team_id}/team")

    async def team_statistics(self, team_id: str) -> dict[str, Any]:
        return await self.get_json(
            f"{ESPN_SITE_API}/teams/{team_id}/statistics", cache_key=f"teams/{team_id}/statistics"
        )

    async def team_schedule(self, team_id: str, season_year: int | None = None) -> dict[str, Any]:
        params = {"season": season_year} if season_year else None
        return await self.get_json(
            f"{ESPN_SITE_API}/teams/{team_id}/schedule", params, cache_key=f"teams/{team_id}/schedule"
        )

    async def team_roster(self, team_id: str) -> dict[str, Any]:
        return await self.get_json(f"{ESPN_SITE_API}/teams/{team_id}/roster", cache_key=f"teams/{team_id}/roster")


def team_id_from_ref(ref: str) -> str:
    """'.../seasons/2026/teams/2?lang=en' -> '2'."""
    tail = ref.split("/teams/", 1)[1]
    return tail.split("?", 1)[0].strip("/")


def group_id_from_ref(ref: str) -> str | None:
    """'.../seasons/2023/types/3/groups/7?lang=en' -> '7'."""
    m = re.search(r"/groups/(\d+)", ref or "")
    return m.group(1) if m else None
