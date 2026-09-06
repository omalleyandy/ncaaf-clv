"""Async AccuWeather Data Service client: rate-limited, retrying, budgeted, with raw-response caching.

Endpoints used (all documented at developer.accuweather.com):
    Locations  /locations/v1/poi/search?q=            stadium POIs -> "53561_POI"-style keys (the web site's `_poi` ids)
               /locations/v1/postalcodes/{cc}/search  postal-code fallback (ESPN core venue carries zipCode)
               /locations/v1/cities/search?q=         city fallback
               /locations/v1/cities/geoposition/search?q=lat,lon
    Forecast   /forecasts/v1/hourly/{N}hour/{key}?details=true&metric=false    N in 1,12 (24/72/120/240 plan-gated)
    Observed   /currentconditions/v1/{key}/historical/24?details=true          past 24 h, hourly (plan-gated on starter)
               /currentconditions/v1/{key}?details=true

Failure semantics (so a run never burns the daily quota on a doomed loop):
    401 -> AccuWeatherAuthError (bad key)            403 -> AccuWeatherPlanError (endpoint not in plan / trial expired)
    429 or 503 "exceeded" -> AccuWeatherQuotaError    5xx -> retry with backoff, then raise
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from ncaaf_clv.common import write_json
from ncaaf_clv.config import ACCUWEATHER_HOURLY_HORIZONS, Settings

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {500, 502, 504}


class AccuWeatherError(RuntimeError):
    """Base class for AccuWeather failures that should stop the run."""


class AccuWeatherAuthError(AccuWeatherError):
    """HTTP 401: the API key is missing or invalid."""


class AccuWeatherPlanError(AccuWeatherError):
    """HTTP 403: the endpoint (or horizon) is not included in the key's plan."""


class AccuWeatherQuotaError(AccuWeatherError):
    """HTTP 429 / 503 'allowed number of requests has been exceeded', or the local call budget is spent."""


class RateLimiter:
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


class AccuWeatherClient:
    """Use as `async with AccuWeatherClient(settings) as c:`. Counts calls; `calls_made` is reported in run meta."""

    def __init__(self, settings: Settings, raw_dir: Path | None = None) -> None:
        if not settings.accuweather_api_key:
            raise AccuWeatherAuthError("ACCUWEATHER_API_KEY is not set (add it to .env)")
        self._settings = settings
        self._raw_dir = raw_dir
        self._limiter = RateLimiter(settings.accuweather_rate_limit_per_s)
        self._sem = asyncio.Semaphore(
            max(1, min(settings.max_concurrency, 2))
        )  # AccuWeather is quota-, not throughput-bound
        self._client: httpx.AsyncClient | None = None
        self.calls_made = 0

    async def __aenter__(self) -> AccuWeatherClient:
        self._client = httpx.AsyncClient(
            base_url=self._settings.accuweather_base_url,
            timeout=self._settings.http_timeout_s,
            headers={"Accept": "application/json", "Accept-Encoding": "gzip"},
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------ core

    def _check_budget(self) -> None:
        budget = self._settings.accuweather_call_budget
        if budget and self.calls_made >= budget:
            raise AccuWeatherQuotaError(f"local call budget of {budget} reached (NCAAF_CLV_ACCUWEATHER_CALL_BUDGET)")

    async def get_json(self, path: str, params: dict[str, Any] | None = None, cache_key: str | None = None) -> Any:
        assert self._client is not None, "use `async with AccuWeatherClient(...)`"
        params = {"apikey": self._settings.accuweather_api_key, **(params or {})}
        attempt = 0
        while True:
            attempt += 1
            self._check_budget()
            async with self._sem:
                await self._limiter.wait()
                self.calls_made += 1
                try:
                    resp = await self._client.get(path, params=params)
                except httpx.HTTPError as exc:
                    if attempt > self._settings.max_retries:
                        raise
                    backoff = 2**attempt
                    log.warning("HTTP error %s on %s (attempt %d); retry in %ss", exc, path, attempt, backoff)
                    await asyncio.sleep(backoff)
                    continue

            if resp.status_code == 200:
                payload = resp.json()
                if cache_key and self._raw_dir is not None:
                    write_json(self._raw_dir / f"{cache_key}.json", payload)
                return payload

            body = resp.text[:300]
            if resp.status_code == 401:
                raise AccuWeatherAuthError(f"HTTP 401 from {path}: {body}")
            if resp.status_code == 403:
                raise AccuWeatherPlanError(f"HTTP 403 from {path} (endpoint not in plan / trial expired): {body}")
            if resp.status_code == 429 or (resp.status_code == 503 and "exceeded" in body.lower()):
                raise AccuWeatherQuotaError(f"HTTP {resp.status_code} from {path}: {body}")
            if resp.status_code in RETRYABLE_STATUS | {503} and attempt <= self._settings.max_retries:
                backoff = 2**attempt
                log.warning("HTTP %d on %s (attempt %d); retry in %ss", resp.status_code, path, attempt, backoff)
                await asyncio.sleep(backoff)
                continue
            resp.raise_for_status()
            raise RuntimeError(f"Unexpected HTTP {resp.status_code} from {path}")

    # ------------------------------------------------------------- locations

    async def poi_search(self, query: str, cache_key: str | None = None) -> list[dict[str, Any]]:
        """Point-of-interest search; stadium results carry keys such as '53561_POI' (Type 'POI')."""
        return await self.get_json("/locations/v1/poi/search", {"q": query, "details": "true"}, cache_key)

    async def postal_search(self, postal_code: str, country_code: str = "US", cache_key: str | None = None) -> list:
        return await self.get_json(
            f"/locations/v1/postalcodes/{country_code}/search", {"q": postal_code, "details": "true"}, cache_key
        )

    async def city_search(self, query: str, cache_key: str | None = None) -> list[dict[str, Any]]:
        return await self.get_json("/locations/v1/cities/search", {"q": query, "details": "true"}, cache_key)

    async def geoposition_search(self, lat: float, lon: float, cache_key: str | None = None) -> dict[str, Any]:
        return await self.get_json(
            "/locations/v1/cities/geoposition/search", {"q": f"{lat},{lon}", "details": "true"}, cache_key
        )

    # -------------------------------------------------------------- forecasts

    async def hourly_forecast(self, location_key: str, hours: int | None = None, cache_key: str | None = None) -> list:
        hours = hours or self._settings.accuweather_hourly_hours
        if hours not in ACCUWEATHER_HOURLY_HORIZONS:
            raise ValueError(f"hourly horizon must be one of {ACCUWEATHER_HOURLY_HORIZONS}, got {hours}")
        return await self.get_json(
            f"/forecasts/v1/hourly/{hours}hour/{location_key}", {"details": "true", "metric": "false"}, cache_key
        )

    async def historical_24h(self, location_key: str, cache_key: str | None = None) -> list[dict[str, Any]]:
        """Observed conditions for the past 24 hours, one item per hour (most recent first)."""
        return await self.get_json(
            f"/currentconditions/v1/{location_key}/historical/24", {"details": "true"}, cache_key
        )

    async def current_conditions(self, location_key: str, cache_key: str | None = None) -> list[dict[str, Any]]:
        return await self.get_json(f"/currentconditions/v1/{location_key}", {"details": "true"}, cache_key)
