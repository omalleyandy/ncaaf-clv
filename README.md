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
| validator | — | `validation/<snapshot>/report.json` |

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
│   ├── storage.py            SQLite loader (INSERT OR REPLACE keyed on natural key + snapshot_utc)
│   ├── schema.sql            tables for every JSON row type + `v_game_lines` convenience view
│   └── cli.py                argparse wiring only
├── tests/                    pytest: parsers, resolver, join, validator
└── data/
    ├── raw/overtime/         Overtime captures (committed sample: ncaaf_lines_20260905T202147Z.json)
    ├── raw/espn/<snapshot>/  raw ESPN payloads (gitignored, for audit/replay)
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
```

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
conference alignment *as of that season*; all-star squads `is_program=false`). No team pages are
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

Sample run (2026-09-05 20:21Z): 99 ESPN games, 138 FBS programs, 18 Overtime games × 3 periods —
18/18 games matched, 36/36 names resolved, 0 errors, 2 warnings (a 90-min kickoff delta on a
delayed game; one 1H team-total pair 3.5 off the 1H total).

## SQLite

`schema.sql` defines a table per row type (`espn_games`, `espn_plays`, `espn_boxscore_player_stats`,
`espn_roster`, `overtime_odds`, `game_map`, …), keyed on the natural key + `snapshot_utc` so repeated
snapshots accumulate as line history. `v_game_lines` joins Overtime full-game lines to the ESPN game
and DraftKings reference for CLV queries.

## Next steps

* Schedule `ncaaf-clv espn` + a browser capture on a cadence (open → hourly → T-15min) to build
  the opening/closing-line history; `overtime_odds` already keys on `snapshot_utc`.
* Register `game_map` into `canonical-team-resolver`'s `canonical.db` so the same ids serve the
  other collectors.
* Feature marts from `espn_team_season_stats` / `espn_plays` (success rate, EPA proxies) to feed
  `xgboost-sports-betting`'s NCAAF schema.
