# ncaaf-clv

NCAAF handicapping data pipeline: ESPN games + teams and Overtime.ag odds, normalised into
SQLite-ready JSON, joined on a deterministic team/game key, and validated — the foundation for
comparing a power-rating model against the market's closing line.

## What it collects

| Source | Page it corresponds to | Output (per snapshot) |
|---|---|---|
| ESPN scoreboard (`group/80` = FBS) | `Card gameModules` on the scoreboard | `scoreboard.json` → `games`, `game_teams`, `espn_odds` |
| ESPN summary per game | **Gamecast** | `games/<game_id>/gamecast.json` → game, teams (linescores, records), scoring plays, leaders, win probability, DraftKings open/close |
| | **Box Score** | `games/<game_id>/boxscore.json` → `team_stats` (EAV), `player_stats` (EAV, `player_id`) |
| | **Play-By-Play** | `games/<game_id>/play_by_play.json` → `drives`, `plays` (down/distance/yardline start+end) |
| ESPN core API odds (backfill) | Gamecast odds strip, all books | `games/<game_id>/gamecast.json` → `espn_odds` (one row per sportsbook; open/close where ESPN kept them) |
| ESPN teams (group 80) | `/college-football/teams` | `teams.json` → team dimension + conferences |
| ESPN team pages | **Statistics** / **Schedule** / **Roster** | `teams/<team_id>/{statistics,schedule,roster}.json` |
| Overtime.ag `COLLEGE FB (1H/2H/Q)` | **GAME / 1ST HALF / 1ST QUARTER** | `overtime/<snapshot>/ncaaf_odds.json` → one row per (game, period) |
| join | — | `joins/<snapshot>/game_map.json` → Overtime `GameNum` ↔ ESPN `game_id`, team map |
| AccuWeather hourly forecast per stadium | `accuweather.com/.../hourly-weather-forecast/<key>_poi` | `weather/<snapshot>/hourly.json` → `weather_hourly` (one row per game × hour around kickoff), `weather_game_window` (per-game coverage + aggregates) |
| AccuWeather observed conditions (past 24 h) | `current-weather` history | same files, `source=accuweather_observed` |
| stadium registry | — | `data/reference/stadium_locations.json` → `weather_locations` (ESPN venue ↔ AccuWeather key, indoor flag) |
| validator | — | `validation/<snapshot>/report.json`, `weather/<snapshot>/report.json` |

Every row carries `source`, `snapshot_utc` and the join keys (`game_id`, `team_id`, `player_id`,
`ot_game_num`). ESPN pages are collected from the JSON endpoints that render those tabs
(`site.api.espn.com/.../summary?event=<id>`, `/teams/<id>/{statistics,schedule,roster}`), which is
deterministic and ~200× cheaper than DOM-clicking each tab.

## Layout

```
ncaaf-clv/
├── pyproject.toml            uv project; `ncaaf-clv` console script
├── .env.example              all settings (NCAAF_CLV_*), defaults documented
├── src/ncaaf_clv/
│   ├── config.py             Settings from env/.env; ESPN base URLs; source constants
│   ├── common.py             snapshot ids, ISO-UTC helpers, JSON I/O, safe numeric parsing
│   ├── espn/client.py        async httpx client: rate limit, retries/backoff, 403/429 detection, raw cache
│   ├── espn/parse.py         scoreboard / gamecast / boxscore / play-by-play / team page parsers
│   ├── espn/collect.py       orchestration -> data/processed/espn/<snapshot>/... (current week + season backfill)
│   ├── overtime/capture.js   browser snippet: dumps the Angular `league.Lines` model (all periods)
│   ├── overtime/parse.py     Overtime lines -> canonical odds rows (favourite + magnitude, ET -> UTC)
│   ├── resolve/team_resolver.py   deterministic name -> ESPN team_id (alias table, then normalised exact match)
│   ├── resolve/aliases.json  sportsbook spellings that normalisation can't resolve (Mississippi -> Ole Miss, ...)
│   ├── resolve/game_join.py  Overtime game -> ESPN game (team pair first, kickoff window as tie-break/bound)
│   ├── validate/validator.py 23 checks: ESPN internal, Overtime internal, join + cross-book agreement
│   ├── weather/client.py     async AccuWeather client: 1 req/s, call budget, 401/403/429 -> typed stop errors, raw cache
│   ├── weather/stadiums.py   ESPN venues -> registry; POI/postal/city resolution; indoor + roof classification
│   ├── weather/parse.py      hourly forecast / observation items -> rows keyed on game_id + team ids; window aggregates
│   ├── weather/collect.py    scope (horizon, indoor, TBA) -> one call per location -> hourly.json
│   ├── weather/validate.py   25 checks: game/team/venue join, coverage, hour window, value ranges
│   ├── replay/build.py       play-by-play -> replay data (clock order, ball-spot repair, win-prob carry) -> HTML
│   ├── replay/template.html  the replay page: field in team colours, score bug, drive chart, transport, drive log
│   ├── storage.py            SQLite loader (INSERT OR REPLACE keyed on natural key + snapshot_utc)
│   ├── schema.sql            tables for every JSON row type + `v_game_lines` convenience view
│   └── cli.py                argparse wiring only
├── tests/                    pytest: parsers, resolver, join, validator
└── data/
    ├── raw/overtime/         Overtime captures (committed sample: ncaaf_lines_20260905T202147Z.json)
    ├── raw/espn/<snapshot>/  raw ESPN payloads (gitignored, for audit/replay)
    ├── raw/accuweather/      raw AccuWeather payloads per weather snapshot (gitignored)
    ├── reference/            stadium_locations.json — committed venue ↔ AccuWeather key registry
    └── processed/<...>       normalised JSON (gitignored)
```

## Run

```bash
uv sync --extra dev
cp .env.example .env            # optional; defaults work

# 1. ESPN: scoreboard (current week, FBS) + every game's summary + every FBS team's pages
uv run ncaaf-clv espn                       # or --date 20260905 --date 20260906
# 2. Overtime.ag: capture in a logged-in browser (see below), then normalise
uv run ncaaf-clv overtime data/raw/overtime/ncaaf_lines_<snapshot>.json --snapshot <snapshot>
# 3. Join + validate
uv run ncaaf-clv join     --espn-snapshot <snapshot> --odds data/processed/overtime/<snapshot>/ncaaf_odds.json
uv run ncaaf-clv validate --espn-snapshot <snapshot> --odds ... --join data/processed/joins/<snapshot>/game_map.json
# or all four steps under one snapshot tag:
uv run ncaaf-clv run data/raw/overtime/ncaaf_lines_<snapshot>.json
# 4. SQLite
uv run ncaaf-clv load-db --db data/ncaaf_clv.db --espn-snapshot <snapshot> --odds ... --join ... --report ...
# 5. Through the week: re-collect the games not yet final (scores, status, box score, plays, closing
#    lines) into a refresh-<tag> snapshot and load it; rows append, `v_espn_games_latest` is current state
uv run ncaaf-clv refresh --db data/ncaaf_clv.db          # --dry-run lists the open games, no ESPN calls
# 6. Watch any collected game: self-contained HTML replay (animated field in team colours, scoreboard,
#    drive chart, ESPN win probability, drive log) -> data/processed/replays/<espn-snapshot>/<game_id>.html
uv run ncaaf-clv replay --espn-snapshot refresh-<tag> --game-id 401856635 --open   # or --all
```

`replay` reads `games/<id>/play_by_play.json` + `gamecast.json` from the snapshot (default: the most
recently written one that has game folders) and writes one page per game with the play data, the team
logos (fetched once from ESPN's CDN into `data/raw/espn/logos/`, `--no-logos` for offline badges) and
the CSS/JS inlined, so the file opens from disk or can be shared as-is. Plays are re-ordered by period
and clock (ESPN lists a timeout after the play it preceded), the ball spot after each play comes from
the next snap (ESPN's `end_yard_line` is 0 on some penalties and turnovers), timeouts and quarter ends
hold the previous state, and the pre-snap clock is parsed from the `(MM:SS)` prefix of the play text so
the scoreboard clock runs during the play.

`refresh` takes the open set from the database (each game's latest `espn_games` row with
`status_state != 'post'`, so cancelled/postponed games are not retried), fetches the scoreboard for the
weeks those games belong to (one call per week), writes scoreboard rows for the open games only, and
collects the full summary plus core-API sportsbook lines for the ones that have kicked off. A game that
was live at one refresh and final at the next keeps both rows. Run it a few times on game days and once
the morning after; there is nothing to fetch once everything is final.

Tests / lint:

```bash
uv run pytest -q
uv run ruff check src tests && uv run ruff format --check src tests
```

## Backfill (past seasons)

```bash
uv run ncaaf-clv backfill --years 2021-2025 --weeks 1-15 --db data/ncaaf_clv.db
```

Walks ESPN's scoreboard by `(year, week, seasontype=2, group=80)` — the JSON behind
`/college-football/scoreboard/_/week/W/year/Y/seasontype/2/group/80` — and collects every game's
summary (Gamecast / Box Score / Play-by-Play) plus the **core API odds strip**
(`events/<id>/competitions/<id>/odds`), which keeps historical sportsbook lines that the summary's
pickcenter no longer carries. Each season is its own snapshot (`data/processed/espn/backfill-<year>/`)
with a season-specific team dimension from `seasons/<year>/teams/<id>` (FBS membership and
conference alignment *as of that season*; all-star squads `is_program=false` and never loaded into
`espn_teams`). No team pages are
collected: the site API only serves the current season's statistics and roster.

- **Resumable.** Re-running skips games already on disk and reuses the season's original
  `snapshot_utc`, so a resumed season still loads as one point-in-time set. `--force` re-fetches.
- **Loads as it goes.** With `--db`, each season is loaded into SQLite when it completes.
- **Odds coverage differs by season** (measured on the 2021–2025 load, ≈ 99 % of games carry a line):
  2024–2025 ESPN BET with explicit open *and* close on ≈ 95–99 % of games; 2023 ESPN BET close on
  97 % (open on about half) and, from week 7, close blocks on Caesars / Unibet / PointsBet too;
  2022 ESPN BET close on 91 % (no open); 2021 `current` lines only, across ≈ 15 books
  (DraftKings, Caesars, MGM, Unibet, consensus …). `home_spread` / `total` are the last line ESPN
  recorded (the de-facto close for a finished game); `close_*` columns stay NULL unless ESPN
  exposed a real close block. A stopped-early run raises `BlockedError` and records the
  missing game ids in `games/_index.json`.
- **User-Agent.** ESPN's Akamai edge returns 403 for bare product tokens such as `ncaaf-clv/0.1`
  (and `Mozilla/5.0` alone) but accepts httpx's default `python-httpx/<ver>`, `curl/…` and full
  desktop UAs. The client therefore sends no custom UA unless `NCAAF_CLV_ESPN_USER_AGENT` is set.
- **Volume.** ≈ 750–900 games per season (FBS vs FBS plus FBS vs FCS), ≈ 400 KB of processed JSON
  per game, two requests per game. Five seasons take roughly 30–60 minutes at the default 8 req/s.

## Overtime.ag capture

Overtime.ag sits behind Cloudflare and requires a login, so the collector works from a
browser session you own. In a logged-in tab with `COLLEGE FB (1H/2H/Q)` open, run
`src/ncaaf_clv/overtime/capture.js` (DevTools console, Playwright `page.evaluate`, or the
Claude-in-Chrome `javascript_tool`) and save the returned JSON under `data/raw/overtime/`.
The page's Angular model already holds **every period** (Game / 1st Half / 1st Quarter …), so
one capture covers all three tabs — no clicking required. The existing
`overtime-ag-collecting` skill's SOAP replay (`GetSportOffering`) returns the same line
objects and can feed `parse_overtime_dump` directly (`{"lines": [...]}`).

Field semantics verified against the rendered UI (2026-09-05):
`Team1`=away, `Team2`=home; `Spread` is the favourite's line with `FavoredTeamID`;
`SpreadAdj1/2`, `MoneyLine1/2`, `TeamN*` follow Team1/Team2; `TtlPtsAdj1`=over, `TtlPtsAdj2`=under;
`GameDateTimeString` is **US/Eastern** (`NCAAF_CLV_OVERTIME_TZ`).

## Join method (Overtime ↔ ESPN)

Game ids and clocks differ between the sources, so the join key is **teams, not time**:

1. Resolve each Overtime name to an ESPN `team_id`: alias table → normalised `location`
   (`Hawai'i`→`hawaii`, `Missouri St`→`missouri state`, `Texas A&M`→`texas a and m`) → other
   ESPN names. No fuzzy matching; unresolved names are reported and belong in `aliases.json`.
   ESPN's all-star squads inside group 80 are flagged `is_program=false` and excluded (they
   otherwise make "South Florida" ambiguous).
2. Candidate ESPN games = same unordered team pair (allows neutral-site home/away swaps, flagged).
3. Nearest kickoff wins; a match outside `NCAAF_CLV_JOIN_WINDOW_HOURS` (12h) is rejected.
   Kickoff deltas are kept in `game_map` (e.g. Tulane–Duke: ESPN 19:30Z *delayed*, Overtime 21:00Z).

## Spread / odds conventions

Per `betting-data-normalizing`: one row per (game, period) with `spread_magnitude` (≥ 0) plus
`favorite_side`/`favorite_team_id`, and signed perspective lines `home_spread` / `away_spread`
(negative = favourite). Totals are a single value with `over_price` / `under_price`. Moneylines
are American ints; convert to implied probability before averaging. ESPN's DraftKings
`open_*`/`close_*` fields are kept as a second-book reference for the cross-book validation check.

## Stadium weather (AccuWeather)

Hourly weather for every **outdoor** FBS game, keyed to the ESPN `game_id`, `home_team_id`, `away_team_id`
and `venue_id`, focused on the kickoff window `[kickoff − 1 h, kickoff + 3.5 h]`
(`NCAAF_CLV_WEATHER_PRE_KICKOFF_HOURS` / `NCAAF_CLV_WEATHER_GAME_DURATION_HOURS`). Indoor stadiums and domes
(ESPN `indoor` flag, a `*dome` name, or the roof table in `weather/stadiums.py` — AT&T, Lucas Oil,
Mercedes-Benz and NRG are retractable, the rest fixed) get a `game_window` row with status
`indoor_skipped` and **no** API call and no hourly rows.

```bash
echo ACCUWEATHER_API_KEY=... >> .env
# 1. registry: every ESPN venue 2021-2026 (172) -> AccuWeather key for the 161 outdoor ones; ~1.3 calls/venue, resumable
uv run ncaaf-clv weather locations --db data/ncaaf_clv.db          # or --espn-snapshot TAG ...; --no-resolve = ESPN only
# 2. pre-game: hourly forecast for games whose window falls inside the next 12 h (one call per stadium)
uv run ncaaf-clv weather forecast --db data/ncaaf_clv.db           # [--espn-snapshot TAG] [--date 20260905] [--game-id ID]
# 3. morning after: what actually happened during the game (past-24 h observations, same columns)
uv run ncaaf-clv weather observed --db data/ncaaf_clv.db
# re-run the checks on any payload
uv run ncaaf-clv weather validate --weather data/processed/weather/<snapshot>/hourly.json
```

**Location resolution** (`data/reference/stadium_locations.json`, committed, hand-editable): POI search on
the stadium name filtered to the venue's state (`53561_POI` is the same id the web site shows as
`.../sanford-stadium/30602/hourly-weather-forecast/53561_poi`), then the ESPN core venue's zip code via
postal-code search, then `City, ST`. Every row records `method`, `query`, candidate count and notes; set
`"method": "manual"` with a `location_key` to pin a venue and it is never overwritten; `--venue-id ID` re-resolves
a single venue. Alias records (`3-53527_1_POI_AL`, `IsAlias=true`) are canonicalised to the site's `53527_POI`
(verified: identical forecast and page link). Renamed venues resolve through former names in `POI_QUERY_ALIASES`
(Sun Devil Stadium, Jones AT&T Stadium, Cajun Field …). Outcome on 2026-09-05 for the 161 outdoor venues seen
2021–2026: 153 stadium POIs, 7 campus zip codes (Husky Stadium, Kenan Stadium, the Ching Complex, UFCU, War
Memorial WY, L.T. Smith, Fifth Third Kennesaw), 1 city (Aviva Stadium, Dublin); every POI's parent city equals
the ESPN venue city. New venues seen in a scoreboard are added and resolved on the fly during `forecast` /
`observed`.

**Snapshots.** Each `forecast` run is one snapshot (`snapshot_utc`), exactly like the odds captures, so
scheduling it at e.g. T-24 h / T-12 h / T-3 h builds a forecast history per game; `lead_hours` on every hour
and `lead_hours_at_kickoff` on the window row say how far out the forecast was. The free/starter tier serves
the **12-hour** horizon only (`NCAAF_CLV_ACCUWEATHER_HOURLY_HOURS`; 24/72/120/240 need a paid plan), so a
Saturday slate needs a late-morning and an afternoon run. The feed starts at the **next full hour** after the
request, so the current hour is never returned; `hours_reachable` on the window row accounts for that. Trial
keys allow 50 calls/day: use `NCAAF_CLV_ACCUWEATHER_CALL_BUDGET` and `--limit`. The client stops the run
(status `api_error`, exit code 2, nothing loaded) on 401 / 403 (plan-gated endpoint) / 429 instead of burning quota.

**Plan gating (measured 2026-09-05 on `core-weather/starter`).** Locations, the 12-hour hourly forecast and the
1-hour endpoint work; `weather observed` (`currentconditions/v1/{key}/historical/24`) and the 24/72/120/240-hour
horizons return 403 with an "upgrade to core-weather/standard" message. Until the plan is upgraded, observed
game-time conditions have to come from Open-Meteo (see below); the module keeps the `observed` command so it
starts working the moment the plan allows it.

**No archived forecasts.** AccuWeather's API has no "what was the forecast for 2023-10-14" endpoint and
`historical/24` only reaches back one day, so 2021–2025 cannot be backfilled from AccuWeather; the
forecast history starts with the first scheduled run. The tables carry a `source` column so an Open-Meteo
archive backfill (free, hourly, 1940+) can slot in as `open_meteo_archive` later — see the
`mlb-weather-reporting` skill for that pattern.

**Rows.** `weather_hourly`: one row per (game, source, hour, snapshot) with `forecast_for_utc`,
`hour_offset_from_kickoff`, `lead_hours`, `in_game_window` plus every `details=true` field — temperature,
RealFeel / RealFeel Shade, wet bulb, dew point, humidity, wind speed / direction / gust, precipitation,
rain, snow, ice and thunderstorm probabilities, liquid / rain / snow / ice amounts, cloud cover, ceiling,
visibility, UV, evapotranspiration, solar irradiance, and the AccuWeather page `link`. Observations add
`pressure_inhg` / `precip_1hr_in` (probabilities are NULL for observed rows). Air quality and AccuLumen shown
on the web page are not in the API. `weather_game_window`: one row per (game, source, snapshot) with
`status` (`forecast` / `observed` / `indoor_skipped` / `unresolved_location` / `outside_horizon` /
`no_kickoff` / `api_error`), `hours_expected` / `hours_reachable` / `hours_present` / `coverage_pct` and
feature-ready aggregates (mean/min/max temperature, temperature at kickoff, max wind and gust, max precip
and thunderstorm probability, liquid sum, hours with precipitation, mean cloud cover, min visibility).
`v_game_weather` joins the latest window per (game, source) to the ESPN game.

First live pass (2026-09-05 23:15Z, mid-slate): 54 games in scope, 48 stadiums called, 124 hourly rows, 3 dome games
skipped, validation PASS apart from the expected in-game coverage notes; e.g. Kyle Field at 23:00Z kickoff:
89 °F, 86 % precipitation probability, 51 % thunderstorm probability, 8 mph gusts.

## Validator

`validate` returns PASS / WARN / FAIL and writes every check with its failing rows:

* **ESPN internal** — unique game ids, both teams present, game files present, FINAL games have
  player rows + plays, box-score ids consistent, team pages present, non-FBS opponents (info).
* **Overtime internal** — one row per (game, period), full-game line present, favourite consistent
  with spread, favourite has the shorter moneyline, totals in range per period, prices in range,
  team totals ≈ total, 1Q < 1H < Game totals and same favourite across periods, kickoff parsed.
* **Join** — every name resolved, every game matched, no ESPN game claimed twice, orientation
  agreement, kickoff agreement (≤ 60 min), and cross-book agreement vs DraftKings (same favourite,
  spread within 3.5, total within 4), plus game status at snapshot (live vs pre-game lines).
* **Weather** (`weather validate`, run automatically after `forecast` / `observed`) — join: every row's
  `game_id` exists, `home_team_id` / `away_team_id` / `venue_id` / `kickoff_utc` equal the ESPN game's, every
  game with hours has a window row; coverage: outdoor games attempted have a location key, indoor games have no
  hours, indoor flag matches the registry, every reachable window hour present (warn), hour counts agree, hours
  contiguous, TBA kickoffs, registry state = venue state, no two venues share a key, games beyond the horizon
  (info), in-game snapshots (info); detail: no duplicate (game, source, hour, snapshot), in-window flags and
  offsets recomputed from the ESPN kickoff, physical ranges (temp −40..130 °F, humidity / probabilities /
  cloud 0..100, wind ≤ 120, gust ≤ 160, UV ≤ 16, pressure 25..32.5 inHg …), required fields, dew point ≤ temp,
  gust ≥ wind, precipitation flags consistent, forecast hours not in the past / observed hours not in the future.

Sample run (2026-09-05 20:21Z): 99 ESPN games, 138 FBS programs, 18 Overtime games × 3 periods —
18/18 games matched, 36/36 names resolved, 0 errors, 2 warnings (a 90-min kickoff delta on a
delayed game; one 1H team-total pair 3.5 off the 1H total).

## SQLite

`schema.sql` defines a table per row type (`espn_games`, `espn_plays`, `espn_boxscore_player_stats`,
`espn_roster`, `overtime_odds`, `game_map`, …), keyed on the natural key + `snapshot_utc` so repeated
snapshots accumulate as line history. `v_espn_games_latest` is each game's row at its latest snapshot
(the current score/status after `refresh`); `v_game_lines` joins Overtime full-game lines to that
current ESPN game row and the DraftKings reference for CLV queries. `weather_locations`, `weather_hourly` and `weather_game_window` hold the
stadium registry and the hourly weather keyed on `(game_id, source, forecast_for_utc, snapshot_utc)`;
`v_game_weather` gives the latest window per game.

## Next steps

* Schedule `ncaaf-clv espn` + a browser capture on a cadence (open → hourly → T-15min) to build
  the opening/closing-line history; `overtime_odds` already keys on `snapshot_utc`.
* Add the AccuWeather key to `.env`, run `weather locations` once (verify a handful of POI keys against the
  web site), then schedule `weather forecast` (Sat ~10:00 ET and ~15:00 ET) and `weather observed` (Sun ~09:00 ET)
  next to the ESPN/odds runs. Backfill 2021–2025 game-time weather from the Open-Meteo archive into the same
  tables (`source=open_meteo_archive`) so weather features exist for the modelling seasons.
* Register `game_map` into `canonical-team-resolver`'s `canonical.db` so the same ids serve the
  other collectors.
* Feature marts from `espn_team_season_stats` / `espn_plays` (success rate, EPA proxies) to feed
  `xgboost-sports-betting`'s NCAAF schema.
