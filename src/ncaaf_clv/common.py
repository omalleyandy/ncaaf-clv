"""Shared helpers: snapshot timestamps, JSON I/O, safe numeric parsing."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SNAPSHOT_FMT = "%Y%m%dT%H%M%SZ"


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def iso_utc(ts: datetime | None = None) -> str:
    """ISO-8601 UTC string with 'Z' suffix (second precision)."""
    ts = ts or utc_now()
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def snapshot_tag(ts: datetime | None = None) -> str:
    """Filesystem-safe snapshot id, e.g. 20260905T201500Z."""
    ts = ts or utc_now()
    return ts.astimezone(UTC).strftime(SNAPSHOT_FMT)


def parse_espn_datetime(value: str | None) -> datetime | None:
    """ESPN emits '2026-09-05T16:00Z' or full ISO with offset."""
    if not value:
        return None
    v = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(v).astimezone(UTC)
    except ValueError:
        log.warning("Unparseable ESPN datetime: %r", value)
        return None


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1, default=str)
    return path


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


_NUM_RE = re.compile(r"^[-+]?\d+(\.\d+)?$")


def to_float(value: Any) -> float | None:
    """Best-effort numeric parse; returns None for '', '-', 'N/A', dicts, etc."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, dict):
        return to_float(value.get("value"))
    s = str(value).strip().replace(",", "")
    if _NUM_RE.match(s):
        return float(s)
    return None


def to_int(value: Any) -> int | None:
    f = to_float(value)
    return int(f) if f is not None else None


def first(items: Iterable[Any], default: Any = None) -> Any:
    for item in items:
        return item
    return default
