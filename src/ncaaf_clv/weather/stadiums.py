"""Stadium registry: ESPN venues -> AccuWeather location keys, with indoor/dome classification.

The venue universe comes from ESPN itself (every `venue_*` block on a scoreboard game), so neutral-site
and bowl venues are covered automatically and the ids line up with `espn_games.venue_id`. Each venue is
resolved once and persisted to `data/reference/stadium_locations.json` (committed, hand-editable):

    1. POI search (`/locations/v1/poi/search`) with progressively simpler queries, each filtered to the
       venue's state/country: full name -> "<X> Field at <Y> Stadium" tail -> last two words -> former
       names of sponsor-renamed venues (POI_QUERY_ALIASES). Result keys look like "53561_POI", the same id
       the web site shows as `.../sanford-stadium/30602/hourly-weather-forecast/53561_poi`. Alias records
       ("3-53527_1_POI_AL", IsAlias=true) are canonicalised to "53527_POI" (verified 2026-09-05: identical
       forecast and Link).                                                        -> method poi_name
    2. postal-code search on the ESPN core venue zipCode                            -> method postal_code
    3. city search "City, ST"                                                       -> method city_state

A hand-set `location_key` in the file (method `manual`) is never overwritten. Indoor = ESPN's `indoor` flag,
a name pattern ("...dome"), or the override table below; indoor games get no forecast rows.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ncaaf_clv.common import iso_utc, read_json, write_json

log = logging.getLogger(__name__)

# ESPN venue_id -> roof type. ESPN flags these `indoor`; the roof type is kept so retractable-roof venues
# can be opted back in later (they play open-roof in early-season heat only rarely for football).
ROOF_OVERRIDES: dict[str, str] = {
    "3687": "retractable",  # AT&T Stadium, Arlington TX
    "3812": "retractable",  # Lucas Oil Stadium, Indianapolis IN
    "5348": "retractable",  # Mercedes-Benz Stadium, Atlanta GA
    "3891": "retractable",  # NRG (Reliant) Stadium, Houston TX
    "6501": "fixed",  # Allegiant Stadium, Las Vegas NV
    "3604": "fixed",  # Alamodome, San Antonio TX
    "3727": "fixed",  # Ford Field, Detroit MI
    "1964": "fixed",  # JMA Wireless Dome, Syracuse NY
    "3714": "fixed",  # Fargodome, Fargo ND
    "3494": "fixed",  # The Dome at America's Center, St. Louis MO
    "3493": "fixed",  # Caesars Superdome, New Orleans LA
}
INDOOR_NAME_RE = re.compile(r"\bdome\b|dome$|superdome|fargodome|alamodome", re.IGNORECASE)

# Former / alternative names for venues whose current sponsor name AccuWeather's POI table does not know.
# Keyed by ESPN venue_id; tried after the generic simplifications, in order.
POI_QUERY_ALIASES: dict[str, list[str]] = {
    "3838": ["Faurot Field"],  # Memorial Stadium, Columbia MO
    "3947": ["Sun Devil Stadium"],  # Mountain America Stadium, Tempe
    "3784": ["Jones AT&T Stadium", "Jones Stadium"],  # Galaxy Stadium, Lubbock
    "3619": ["Arizona Stadium"],  # Casino Del Sol Stadium, Tucson
    "3835": ["Oklahoma Memorial Stadium", "Gaylord Family Oklahoma Memorial Stadium"],
    "3765": ["Alaska Airlines Field"],  # Husky Stadium, Seattle
    "3873": ["Cardinal Stadium", "Papa John's Cardinal Stadium"],  # L&N FCU Stadium, Louisville
    "3973": ["Vanderbilt Stadium"],  # FirstBank Stadium
    "6577": ["High Point Solutions Stadium", "Rutgers Stadium"],  # SHI Stadium
    "3653": ["Bronco Stadium"],  # Albertsons Stadium, Boise
    "3666": ["Cajun Field"],  # Our Lady of Lourdes Stadium, Lafayette LA
    "5388": ["Colorado State Stadium", "Sonny Lubick Field"],  # Canvas Stadium
    "3965": ["UB Stadium"],  # Broadview Stadium, Buffalo
    "3644": ["Bobcat Stadium"],  # UFCU Stadium, San Marcos
    "3715": ["FAU Stadium"],  # Flagler Credit Union Stadium, Boca Raton
    "3825": ["Apogee Stadium"],  # DATCU Stadium, Denton
    "3905": ["Romney Stadium"],  # Maverik Stadium, Logan
    "3918": ["Foreman Field"],  # S.B. Ballard Stadium, Norfolk
    "3935": ["Spartan Stadium"],  # CEFCU Stadium, San Jose
    "4246": ["Georgia State Stadium", "Turner Field"],  # Center Parc Stadium
    "3660": ["Bulldog Stadium"],  # Valley Children's Stadium, Fresno
    "3796": ["L.T. Smith Stadium", "Smith Stadium"],  # Houchens Industries-L.T. Smith Stadium
    "3725": ["Floyd Stadium"],  # Johnny "Red" Floyd Stadium
    "3786": ["Kelly Shorts Stadium"],
    "3833": ["Kansas Memorial Stadium", "Memorial Stadium"],  # David Booth Kansas Memorial Stadium
    "3696": ["Dix Stadium"],  # Zoeller Field at Dix Stadium
    "3662": ["Burgess-Snow Field"],  # AmFirst Stadium, Jacksonville AL
    "218": ["FIU Stadium"],  # Riccardo Silva Stadium
    "2031": ["FIU Stadium", "Riccardo Silva Stadium"],  # Pitbull Stadium (same venue, renamed)
    "4899": ["Fifth Third Stadium"],  # Walens Family Field at Fifth Third Stadium, Kennesaw
    "5960": ["Martin Stadium", "Ryan Field"],  # Northwestern Medicine Field at Martin Stadium, Evanston
    "3647": ["Bowers Stadium"],  # Elliott T. Bowers Stadium, Huntsville TX
    "538": ["StubHub Center", "Home Depot Center"],  # Dignity Health Sports Park, Carson
    "3878": ["Plaster Stadium"],  # Robert W. Plaster Stadium, Springfield MO
    "3631": ["BBVA Stadium", "BBVA Compass Stadium"],  # Shell Energy Stadium, Houston
    "3712": ["TIAA Bank Field", "EverBank Field"],  # EverBank Stadium, Jacksonville
    "4258": ["Globe Life Park"],  # Choctaw Stadium, Arlington
    "3673": ["CenturyLink Field"],  # Lumen Field, Seattle
    "3874": ["Paul Brown Stadium"],  # Paycor Stadium, Cincinnati
    "3486": ["Children's Mercy Park", "Sporting Park"],  # Kansas City KS
    "3910": ["Darrell K Royal-Texas Memorial Stadium", "Texas Memorial Stadium"],  # DKR-Texas Memorial Stadium
    "3616": ["Amon Carter Stadium"],
    "3887": ["Razorback Stadium"],
    "3985": ["McGuirk Alumni Stadium", "McGuirk Stadium"],
    "3700": ["Doyt Perry Stadium"],
    "3608": ["Paulson Stadium"],
    "3735": ["Ford Stadium"],
    "3674": ["Chapman Stadium", "Skelly Field"],
    "3815": ["Roberts Stadium"],
    "3775": ["Edwards Stadium"],
    "3911": ["Ryan Field"],  # Ryan Field (1926)
    "3937": ["Empower Field", "Mile High Stadium", "Sports Authority Field"],
    "3810": ["Nissan Stadium", "LP Field"],
    "3757": ["Hornet Stadium"],
    "7221": ["Protective Stadium"],
    "4013": ["Camping World Stadium", "Citrus Bowl"],
}

COUNTRY_CODES = {
    "USA": "US",
    "United States": "US",
    "Ireland": "IE",
    "Canada": "CA",
    "Mexico": "MX",
    "Australia": "AU",
    "Germany": "DE",
    "Japan": "JP",
    "England": "GB",
    "United Kingdom": "GB",
}

# ESPN venue ids that are the same physical stadium (renamed), so sharing one AccuWeather key is expected.
SAME_STADIUM_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"218", "2031"}),  # Riccardo Silva Stadium / Pitbull Stadium (FIU)
    frozenset({"3911", "5960"}),  # Ryan Field (1926) / Northwestern Medicine Field at Martin Stadium (site rebuild)
)


def same_stadium(venue_ids: Iterable[str]) -> bool:
    ids = {str(v) for v in venue_ids}
    return any(ids <= group for group in SAME_STADIUM_GROUPS)


MANUAL_METHOD = "manual"
RESOLVED_METHODS = frozenset({"poi_name", "postal_code", "city_state", "geoposition", MANUAL_METHOD})
ALIAS_KEY_RE = re.compile(r"^\d+-(\d+)_\d+_POI_AL$", re.IGNORECASE)
ACCUWEATHER_WEB = "https://www.accuweather.com"


def country_code(name: str | None) -> str | None:
    if not name:
        return None
    return COUNTRY_CODES.get(name, name if len(name) == 2 else None)


def is_indoor_venue(venue: dict[str, Any]) -> tuple[bool, str | None]:
    """(is_indoor, roof_type). roof_type: 'fixed' | 'retractable' | None (open-air)."""
    vid = str(venue.get("venue_id") or "")
    roof = ROOF_OVERRIDES.get(vid)
    if roof:
        return True, roof
    if venue.get("venue_indoor"):
        return True, "fixed"
    if INDOOR_NAME_RE.search(venue.get("venue_name") or ""):
        return True, "fixed"
    return False, None


def clean_poi_query(venue_name: str) -> str:
    """Drop ESPN's disambiguating parenthetical: 'Tiger Stadium (LA)' / 'Memorial Stadium (Lincoln, NE)' /
    'Ryan Field (1926)' -> 'Tiger Stadium' / 'Memorial Stadium' / 'Ryan Field'. The state filter in
    `pick_location` does the disambiguation; AccuWeather's search does not accept a city suffix."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", venue_name or "").strip()


def poi_queries(venue: dict[str, Any]) -> list[str]:
    """Ordered, de-duplicated POI queries for a venue: full name, the '<Field> at <Stadium>' tail, the last
    two words ('Donald W. Reynolds Razorback Stadium' -> 'Razorback Stadium'), then known former names."""
    base = clean_poi_query(venue.get("venue_name") or "")
    out: list[str] = []

    def add(q: str) -> None:
        q = q.strip()
        if q and q.lower() not in {x.lower() for x in out}:
            out.append(q)

    add(base)
    if " at " in base:
        add(base.split(" at ", 1)[1])
    words = base.replace("/", " ").split()
    if len(words) >= 3:
        add(" ".join(words[-2:]))
    for alias in POI_QUERY_ALIASES.get(str(venue.get("venue_id")), []):
        add(alias)
    return out


# ------------------------------------------------------------------- venue universe


def venues_from_games(games: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Distinct venues keyed by venue_id from ESPN game rows (latest non-null attribute wins)."""
    out: dict[str, dict[str, Any]] = {}
    for g in games:
        vid = g.get("venue_id")
        if not vid:
            continue
        v = out.setdefault(
            str(vid),
            {
                "venue_id": str(vid),
                "venue_name": None,
                "venue_city": None,
                "venue_state": None,
                "venue_country": None,
                "venue_indoor": None,
                "game_count": 0,
                "first_season": None,
                "last_season": None,
            },
        )
        for k in ("venue_name", "venue_city", "venue_state", "venue_country"):
            if g.get(k):
                v[k] = g[k]
        if g.get("venue_indoor") is not None:
            v["venue_indoor"] = bool(v["venue_indoor"]) or bool(g["venue_indoor"])
        v["game_count"] += 1
        yr = g.get("season_year")
        if yr is not None:
            v["first_season"] = yr if v["first_season"] is None else min(v["first_season"], yr)
            v["last_season"] = yr if v["last_season"] is None else max(v["last_season"], yr)
    return out


def venues_from_snapshots(espn_dirs: Iterable[Path]) -> dict[str, dict[str, Any]]:
    games: list[dict[str, Any]] = []
    for d in espn_dirs:
        sb = d / "scoreboard.json"
        if sb.exists():
            games.extend(read_json(sb)["games"])
    return venues_from_games(games)


def venues_from_db(db_path: Path) -> dict[str, dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT venue_id, venue_name, venue_city, venue_state, venue_country, venue_indoor, season_year "
            "FROM espn_games WHERE venue_id IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    return venues_from_games(dict(r) for r in rows)


# --------------------------------------------------------------------- registry file

LOCATION_FIELDS = (
    "location_key",
    "alias_key",
    "is_alias",
    "location_name",
    "location_type",
    "admin_area",
    "country_code",
    "primary_postal_code",
    "parent_city",
    "lat",
    "lon",
    "elevation_ft",
    "tz_name",
    "tz_offset_h",
    "location_stem",
    "web_hourly_url",
)


def empty_registry() -> dict[str, Any]:
    return {"meta": {"source": "accuweather", "updated_utc": None, "venue_count": 0}, "venues": []}


def load_registry(path: Path) -> dict[str, Any]:
    return read_json(path) if path.exists() else empty_registry()


def save_registry(path: Path, registry: dict[str, Any]) -> Path:
    registry["venues"].sort(key=lambda v: (not v.get("is_indoor"), -(v.get("game_count") or 0), v["venue_id"]))
    methods = sorted({v.get("method") for v in registry["venues"] if v.get("location_key")}, key=str)
    registry["meta"].update(
        {
            "updated_utc": iso_utc(),
            "venue_count": len(registry["venues"]),
            "resolved": sum(1 for v in registry["venues"] if v.get("location_key")),
            "by_method": {
                m: sum(1 for v in registry["venues"] if v.get("location_key") and v.get("method") == m) for m in methods
            },
            "indoor": sum(1 for v in registry["venues"] if v.get("is_indoor")),
            "unresolved_outdoor": sum(
                1 for v in registry["venues"] if not v.get("is_indoor") and not v.get("location_key")
            ),
        }
    )
    return write_json(path, registry)


def registry_index(registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {v["venue_id"]: v for v in registry.get("venues", [])}


def merge_venues(registry: dict[str, Any], venues: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Add newly seen venues and refresh ESPN attributes; never touches resolved location fields."""
    idx = registry_index(registry)
    for vid, v in venues.items():
        indoor, roof = is_indoor_venue(v)
        row = idx.get(vid)
        if row is None:
            row = {"venue_id": vid, **dict.fromkeys(LOCATION_FIELDS)}
            row.update({"method": None, "query": None, "candidates": None, "resolved_utc": None, "notes": []})
            registry["venues"].append(row)
            idx[vid] = row
        for f in LOCATION_FIELDS:
            row.setdefault(f, None)
        row.update(
            {
                "venue_name": v.get("venue_name") or row.get("venue_name"),
                "venue_city": v.get("venue_city") or row.get("venue_city"),
                "venue_state": v.get("venue_state") or row.get("venue_state"),
                "venue_country": v.get("venue_country") or row.get("venue_country"),
                "venue_zip": row.get("venue_zip"),
                "venue_indoor": v.get("venue_indoor"),
                "roof_type": roof,
                "is_indoor": indoor,
                "game_count": v.get("game_count"),
                "first_season": v.get("first_season"),
                "last_season": v.get("last_season"),
            }
        )
    return registry


# ------------------------------------------------------------------- candidate picking


def canonical_key(key: str | None) -> tuple[str | None, str | None]:
    """('53527_POI', '3-53527_1_POI_AL') for an alias key; (key, None) otherwise."""
    if not key:
        return None, None
    m = ALIAS_KEY_RE.match(key)
    return (f"{m.group(1)}_POI", key) if m else (key, None)


def _loc_fields(loc: dict[str, Any]) -> dict[str, Any]:
    geo = loc.get("GeoPosition") or {}
    tz = loc.get("TimeZone") or {}
    elev = ((geo.get("Elevation") or {}).get("Imperial") or {}).get("Value")
    key, alias = canonical_key(loc.get("Key"))
    stem = (loc.get("Details") or {}).get("LocationStem") if isinstance(loc.get("Details"), dict) else None
    return {
        "location_key": key,
        "alias_key": alias,
        "is_alias": bool(loc.get("IsAlias")),
        "location_name": loc.get("LocalizedName") or loc.get("EnglishName"),
        "location_type": loc.get("Type"),
        "admin_area": (loc.get("AdministrativeArea") or {}).get("ID"),
        "country_code": (loc.get("Country") or {}).get("ID"),
        "primary_postal_code": loc.get("PrimaryPostalCode"),
        "parent_city": (loc.get("ParentCity") or {}).get("LocalizedName"),
        "lat": geo.get("Latitude"),
        "lon": geo.get("Longitude"),
        "elevation_ft": elev,
        "tz_name": tz.get("Name"),
        "tz_offset_h": tz.get("GmtOffset"),
        "location_stem": stem,
        "web_hourly_url": f"{ACCUWEATHER_WEB}/en/{stem}/hourly-weather-forecast/{key.lower()}"
        if stem and key
        else None,
    }


def _matches_place(loc: dict[str, Any], venue: dict[str, Any]) -> bool:
    cc = country_code(venue.get("venue_country"))
    loc_cc = (loc.get("Country") or {}).get("ID")
    if cc and loc_cc and cc != loc_cc:
        return False
    state = venue.get("venue_state")
    if state and cc == "US":
        return (loc.get("AdministrativeArea") or {}).get("ID") == state
    return True


def _city_names(loc: dict[str, Any]) -> set[str]:
    names = {(loc.get("ParentCity") or {}).get("LocalizedName"), (loc.get("ParentCity") or {}).get("EnglishName")}
    for area in loc.get("SupplementalAdminAreas") or []:
        names.add(area.get("LocalizedName"))
    return {n.lower() for n in names if n}


def _matches_city(loc: dict[str, Any], city: str | None) -> bool:
    """'Lexington' matches parent city 'Lexington-Fayette'; 'Athens' does not match 'Clarke' (county)."""
    if not city:
        return False
    c = city.lower()
    return any(n == c or n.startswith(c + "-") or n.startswith(c + " ") for n in _city_names(loc))


def pick_location(candidates: list[dict[str, Any]], venue: dict[str, Any], prefer_name: str | None = None) -> tuple:
    """Deterministic choice among API candidates: same country/state; then the venue's city; then exact
    name; then non-alias records. Returns (chosen or None, notes)."""
    notes: list[str] = []
    if not candidates:
        return None, ["no candidates"]
    pool = [c for c in candidates if _matches_place(c, venue)]
    if not pool:
        notes.append(f"{len(candidates)} candidates, none in {venue.get('venue_state') or venue.get('venue_country')}")
        return None, notes
    if len(pool) > 1:
        in_city = [c for c in pool if _matches_city(c, venue.get("venue_city"))]
        if in_city:
            pool = in_city
    if len(pool) > 1 and prefer_name:
        target = prefer_name.strip().lower()
        exact = [c for c in pool if (c.get("LocalizedName") or c.get("EnglishName") or "").strip().lower() == target]
        if exact:
            pool = exact
    if len(pool) > 1:
        canonical = [c for c in pool if not c.get("IsAlias")]
        if canonical:
            pool = canonical
    if len(pool) > 1:
        notes.append(
            f"{len(pool)} candidates in place; took first: "
            + "; ".join(f"{c.get('Key')}={c.get('LocalizedName')}" for c in pool[:5])
        )
    return pool[0], notes


# ------------------------------------------------------------------------- resolution


def _clear_location(venue: dict[str, Any]) -> None:
    for f in LOCATION_FIELDS:
        venue[f] = None


async def resolve_venue(client, espn_client, venue: dict[str, Any]) -> dict[str, Any]:
    """Fill `venue`'s location fields via POI queries -> postal -> city search. Mutates and returns the row."""
    from ncaaf_clv.weather.client import AccuWeatherError  # noqa: PLC0415 - avoid import cycle at module load

    cc = country_code(venue.get("venue_country")) or "US"
    notes: list[str] = []
    chosen = None
    method = None
    query = None
    n_candidates = 0
    _clear_location(venue)

    # 1. POI by (progressively simpler) stadium names
    for i, q in enumerate(poi_queries(venue)):
        query = q
        try:
            cands = await client.poi_search(q, cache_key=f"locations/poi_{venue['venue_id']}_{i}")
        except AccuWeatherError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep trying other strategies
            notes.append(f"poi search {q!r} failed: {exc}")
            continue
        n_candidates = len(cands or [])
        chosen, n = pick_location(cands or [], venue, prefer_name=q)
        if chosen:
            method = "poi_name"
            notes += n
            break
        notes.append(f"poi {q!r}: " + (n[0] if n else "no match"))

    # 2. postal code (ESPN core venue)
    if chosen is None and espn_client is not None and cc == "US":
        try:
            core = await espn_client.venue(venue["venue_id"])
            venue["venue_zip"] = (core.get("address") or {}).get("zipCode") or venue.get("venue_zip")
            if core.get("indoor") is not None:
                venue["venue_indoor"] = bool(core["indoor"]) or bool(venue.get("venue_indoor"))
        except Exception as exc:  # noqa: BLE001
            notes.append(f"espn venue lookup failed: {exc}")
        if venue.get("venue_zip"):
            query = str(venue["venue_zip"])
            cands = await client.postal_search(query, cc, cache_key=f"locations/postal_{venue['venue_id']}")
            n_candidates = len(cands or [])
            chosen, n = pick_location(cands or [], venue)
            notes += n
            if chosen:
                method = "postal_code"

    # 3. city
    if chosen is None and venue.get("venue_city"):
        query = ", ".join(p for p in (venue.get("venue_city"), venue.get("venue_state")) if p)
        cands = await client.city_search(query, cache_key=f"locations/city_{venue['venue_id']}")
        n_candidates = len(cands or [])
        chosen, n = pick_location(cands or [], venue, prefer_name=venue.get("venue_city"))
        notes += n
        if chosen:
            method = "city_state"

    if chosen is not None:
        venue.update(_loc_fields(chosen))
    venue.update(
        {"method": method, "query": query, "candidates": n_candidates, "resolved_utc": iso_utc(), "notes": notes}
    )
    is_indoor, roof = is_indoor_venue(venue)
    venue["is_indoor"], venue["roof_type"] = is_indoor, roof
    return venue


async def resolve_registry(
    client,
    espn_client,
    registry: dict[str, Any],
    only_unresolved: bool = True,
    include_indoor: bool = False,
    limit: int | None = None,
    venue_ids: Iterable[str] | None = None,
) -> dict[str, int]:
    """Resolve venues in place. Indoor venues are skipped by default (no forecast needed); manual keys are
    never overwritten; `venue_ids` re-resolves exactly those venues regardless of state. Stops cleanly on
    auth/plan/quota errors and keeps progress."""
    from ncaaf_clv.weather.client import AccuWeatherError  # noqa: PLC0415

    wanted = {str(v) for v in venue_ids} if venue_ids else None
    todo = []
    for v in registry["venues"]:
        if v.get("method") == MANUAL_METHOD and v.get("location_key"):
            continue
        if wanted is not None:
            if v["venue_id"] in wanted:
                todo.append(v)
            continue
        if v.get("is_indoor") and not include_indoor:
            continue
        if only_unresolved and v.get("location_key"):
            continue
        todo.append(v)
    todo.sort(key=lambda v: -(v.get("game_count") or 0))
    if limit:
        todo = todo[:limit]
    stats = {"attempted": 0, "resolved": 0, "unresolved": 0, "stopped": 0}
    for v in todo:
        stats["attempted"] += 1
        try:
            await resolve_venue(client, espn_client, v)
        except AccuWeatherError as exc:
            log.error("stopping location resolution: %s", exc)
            stats["stopped"] = 1
            v.setdefault("notes", []).append(str(exc))
            break
        if v.get("location_key"):
            stats["resolved"] += 1
            log.info(
                "venue %s %s -> %s (%s via %r)",
                v["venue_id"],
                v.get("venue_name"),
                v["location_key"],
                v["method"],
                v["query"],
            )
        else:
            stats["unresolved"] += 1
            log.warning("venue %s %s unresolved: %s", v["venue_id"], v.get("venue_name"), v.get("notes"))
    return stats
