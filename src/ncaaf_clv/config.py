"""Configuration for the NCAAF CLV pipeline, loaded from environment / .env."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ENV_PREFIX = "NCAAF_CLV_"

ESPN_SITE_API = "https://site.api.espn.com/apis/site/v2/sports/football/college-football"
ESPN_CORE_API = "https://sports.core.api.espn.com/v2/sports/football/leagues/college-football"
ESPN_WEB_API = "https://site.web.api.espn.com/apis/site/v2/sports/football/college-football"

FBS_GROUP_ID = 80
REGULAR_SEASON_TYPE = 2

SOURCE_ESPN = "espn"
SOURCE_OVERTIME = "overtime_ag"


def _env(name: str, default: str) -> str:
    return os.environ.get(f"{ENV_PREFIX}{name}", default)


@dataclass(frozen=True)
class Settings:
    """Runtime settings. All values are overridable via NCAAF_CLV_* env vars."""

    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "data")))
    season_year: int = field(default_factory=lambda: int(_env("SEASON_YEAR", "2026")))
    espn_group_id: int = field(default_factory=lambda: int(_env("ESPN_GROUP_ID", str(FBS_GROUP_ID))))
    http_timeout_s: float = field(default_factory=lambda: float(_env("HTTP_TIMEOUT_S", "30")))
    max_concurrency: int = field(default_factory=lambda: int(_env("MAX_CONCURRENCY", "6")))
    rate_limit_per_s: float = field(default_factory=lambda: float(_env("RATE_LIMIT_PER_S", "8")))
    max_retries: int = field(default_factory=lambda: int(_env("MAX_RETRIES", "4")))
    overtime_tz: str = field(default_factory=lambda: _env("OVERTIME_TZ", "America/New_York"))
    join_window_hours: float = field(default_factory=lambda: float(_env("JOIN_WINDOW_HOURS", "12")))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))
    # ESPN's Akamai edge returns 403 for custom UA strings such as "ncaaf-clv/0.1" (verified 2026-09-05);
    # empty = use httpx's default "python-httpx/<ver>", which is accepted.
    espn_user_agent: str = field(default_factory=lambda: _env("ESPN_USER_AGENT", ""))

    # Derived paths
    @property
    def raw_espn_dir(self) -> Path:
        return self.data_dir / "raw" / "espn"

    @property
    def raw_overtime_dir(self) -> Path:
        return self.data_dir / "raw" / "overtime"

    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "processed"


def load_settings(dotenv_path: str | os.PathLike[str] | None = None) -> Settings:
    """Load .env (if present) and build Settings."""
    load_dotenv(dotenv_path=dotenv_path, override=False)
    return Settings()


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
