"""Deterministic Overtime.ag -> ESPN team resolution (no fuzzy matching).

Resolution order for a raw sportsbook name:
    1. explicit alias table (aliases.json: raw name -> ESPN team_id)
    2. exact match on normalised ESPN `location`   (e.g. "Texas A&M", "Ole Miss")
    3. exact match on normalised short_display_name / display_name / abbreviation / slug
Anything else is reported as unresolved; add it to aliases.json rather than loosening the match.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ALIASES_PATH = Path(__file__).with_name("aliases.json")

# Whole-token expansions applied after lower-casing / punctuation stripping.
TOKEN_MAP: dict[str, str] = {
    "st": "state",
    "so": "southern",
    "sou": "southern",
    "no": "northern",
    "nor": "northern",
    "e": "east",
    "w": "west",
    "n": "north",
    "s": "south",
    "intl": "international",
    "univ": "university",
    "fla": "florida",
    "mich": "michigan",
    "tenn": "tennessee",
    "miss": "mississippi",
    "ala": "alabama",
    "caro": "carolina",
    "ky": "kentucky",
    "la": "louisiana",
    "tx": "texas",
    "ill": "illinois",
    "ariz": "arizona",
    "wash": "washington",
    "okla": "oklahoma",
    "va": "virginia",
    "ga": "georgia",
}

_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")


def normalize_name(name: str | None) -> str:
    """'Hawai'i' -> 'hawaii', 'Missouri St' -> 'missouri state', 'Texas A&M' -> 'texas a and m'."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = s.lower().replace("&", " and ").replace("-", " ").replace("'", "").replace(".", "")
    s = _PUNCT_RE.sub(" ", s)
    tokens = [TOKEN_MAP.get(t, t) for t in s.split()]
    return " ".join(tokens)


@dataclass
class ResolvedTeam:
    raw_name: str
    team_id: str | None
    method: str
    espn_display_name: str | None = None
    candidates: list[str] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return self.team_id is not None


@dataclass
class TeamResolver:
    """Index ESPN team dimension rows and resolve sportsbook names to ESPN team ids."""

    teams: list[dict[str, Any]]
    aliases: dict[str, str] = field(default_factory=dict)
    _by_id: dict[str, dict[str, Any]] = field(init=False, default_factory=dict)
    _by_location: dict[str, list[str]] = field(init=False, default_factory=dict)
    _by_alt: dict[str, list[str]] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        for t in self.teams:
            if not t.get("is_program", True):
                continue
            tid = str(t["team_id"])
            self._by_id[tid] = t
            self._by_location.setdefault(normalize_name(t.get("location")), []).append(tid)
            alts = {
                t.get("short_display_name"),
                t.get("display_name"),
                t.get("abbreviation"),
                (t.get("slug") or "").replace("-", " "),
                f"{t.get('location', '')} {t.get('nickname', '')}".strip(),
            }
            for alt in alts:
                key = normalize_name(alt)
                if key:
                    self._by_alt.setdefault(key, []).append(tid)

    @classmethod
    def from_files(cls, teams_json: Path, aliases_json: Path = ALIASES_PATH) -> TeamResolver:
        with teams_json.open(encoding="utf-8") as fh:
            payload = json.load(fh)
        aliases = load_aliases(aliases_json)
        return cls(teams=payload["teams"], aliases=aliases)

    def team(self, team_id: str) -> dict[str, Any] | None:
        return self._by_id.get(str(team_id))

    def resolve(self, raw_name: str) -> ResolvedTeam:
        alias_hit = self.aliases.get(raw_name) or self.aliases.get(normalize_name(raw_name))
        if alias_hit and alias_hit in self._by_id:
            return ResolvedTeam(raw_name, alias_hit, "alias", self._by_id[alias_hit].get("display_name"))
        key = normalize_name(raw_name)
        for method, index in (("location", self._by_location), ("alt_name", self._by_alt)):
            hits = sorted(set(index.get(key, [])))
            if len(hits) == 1:
                return ResolvedTeam(raw_name, hits[0], method, self._by_id[hits[0]].get("display_name"))
            if len(hits) > 1:
                return ResolvedTeam(raw_name, None, f"ambiguous_{method}", None, hits)
        return ResolvedTeam(raw_name, None, "unresolved")


def load_aliases(path: Path = ALIASES_PATH) -> dict[str, str]:
    """aliases.json: {"<raw name>": {"team_id": "145", "espn": "Ole Miss Rebels"}, ...}
    Keys are matched both verbatim and normalised."""
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    out: dict[str, str] = {}
    for raw, entry in data.get("overtime_ag", {}).items():
        tid = str(entry["team_id"] if isinstance(entry, dict) else entry)
        out[raw] = tid
        out[normalize_name(raw)] = tid
    return out
