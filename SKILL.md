---
name: ncaaf-clv
description: NCAAF handicapping data pipeline — collect ESPN FBS scoreboard, per-game Gamecast/Box Score/Play-by-Play and per-team Statistics/Schedule/Roster as SQLite-ready JSON; normalise Overtime.ag COLLEGE FB Game / 1st Half / 1st Quarter odds; join odds to ESPN games by a deterministic team-pair key; collect AccuWeather hourly forecasts and observed conditions around kickoff for every outdoor FBS stadium (indoor/domes skipped) keyed to ESPN game and team ids; validate games, teams, odds and weather; load everything into SQLite for closing-line-value analysis. Use whenever a task involves college football (NCAAF/FBS) game data, team pages, Overtime.ag college football lines, joining sportsbook lines to ESPN ids, stadium weather / AccuWeather forecasts for college football games, or CLV tracking for college football.
---

# ncaaf-clv

Foundation for comparing an NCAAF power-rating model against the market's closing line.
Full documentation, conventions and the join method are in `README.md`; this file is the
operating card.

## Quick start

```bash
cd skills/ncaaf-clv
uv sync --extra dev                      # standalone; or `uv sync --all-packages` from the workspace root

uv run ncaaf-clv espn                    # FBS scoreboard (current week) + every game + every team
uv run ncaaf-clv overtime data/raw/overtime/ncaaf_lines_<snap>.json --snapshot <snap>
uv run ncaaf-clv join     --espn-snapshot <snap> --odds data/processed/overtime/<snap>/ncaaf_odds.json
uv run ncaaf-clv validate --espn-snapshot <snap> --odds ... --join data/processed/joins/<snap>/game_map.json
uv run ncaaf-clv load-db  --db data/ncaaf_clv.db --espn-snapshot <snap> --odds ... --join ... --report ...
# or all steps under one snapshot tag:
uv run ncaaf-clv run data/raw/overtime/ncaaf_lines_<snap>.json
# past seasons by (year, week) with core-API sportsbook lines; resumable, loads per season:
uv run ncaaf-clv backfill --years 2021-2025 --weeks 1-15 --db data/ncaaf_clv.db
# through the week: re-collect games not yet final (appends a refresh-<snap> snapshot; v_espn_games_latest = current state)
uv run ncaaf-clv refresh --db data/ncaaf_clv.db          # --dry-run lists the open games
# weather (ACCUWEATHER_API_KEY in .env): registry once, then pre-game forecast + morning-after observations
uv run ncaaf-clv weather locations --db data/ncaaf_clv.db
uv run ncaaf-clv weather forecast  --db data/ncaaf_clv.db      # games in the next 12 h (starter tier horizon)
uv run ncaaf-clv weather observed  --db data/ncaaf_clv.db      # games played in the last 24 h
```

## Capturing Overtime.ag lines

Overtime.ag is Cloudflare-fronted and login-gated, so the capture runs in a browser you own
(Claude in Chrome, Playwright, or DevTools). With `COLLEGE FB (1H/2H/Q)` open, evaluate
`src/ncaaf_clv/overtime/capture.js`; it returns JSON holding **every period** from the page's
Angular model, so one capture covers GAME / 1ST HALF / 1ST QUARTER. Save it as
`data/raw/overtime/ncaaf_lines_<YYYYMMDDTHHMMSSZ>.json`. The `overtime-ag-collecting` skill's
`GetSportOffering` replay returns the same line objects and can be fed as `{"lines": [...]}`.

## Conventions (non-negotiable)

- `betting-data-normalizing`: one row per (game, period); `spread_magnitude` ≥ 0 + `favorite_side`;
  signed `home_spread` / `away_spread` (negative = favourite); single `total`; American moneylines.
- Overtime `Team1` = away, `Team2` = home; `GameDateTimeString` is US/Eastern.
- Join key is the **team pair**, not the clock (kickoffs differ across sources); kickoff is a
  tie-break bounded by `NCAAF_CLV_JOIN_WINDOW_HOURS`. Unresolved names go in
  `src/ncaaf_clv/resolve/aliases.json` — never loosen the matcher.
- Every row carries `snapshot_utc`; SQLite keys are natural key + `snapshot_utc`, so repeated
  captures accumulate as line history.
- Refresh snapshots are `refresh-<snap>` and hold only the games still open in SQLite (no team
  dimension); they append, never overwrite, and `v_espn_games_latest` picks each game's newest row.
  Weather and other "latest snapshot" defaults skip `refresh-*` and `backfill-*` dirs.
- Backfill snapshots are `backfill-<year>`, one per season, with that season's team dimension
  (conference alignment as of the year). `espn_odds.close_*` is NULL unless ESPN exposed a real
  close block (ESPN BET 2022+, open blocks 2023+); `home_spread` / `total` are the last recorded line.
- Send no custom User-Agent to ESPN: the edge 403s bare product tokens (`ncaaf-clv/0.1`).
- Weather rows are keyed `(game_id, source, forecast_for_utc, snapshot_utc)` and carry the ESPN home/away team ids
  and `venue_id`; only hours in `[kickoff-1h, kickoff+3.5h]` are stored. Indoor / dome venues (ESPN flag, `*dome`
  name, roof table in `weather/stadiums.py`) get `status=indoor_skipped` and no API call. The stadium registry
  `data/reference/stadium_locations.json` is committed; pin a venue with `"method": "manual"`. AccuWeather keeps
  no archived forecasts (`historical/24` = past day only): forecast history starts at the first scheduled run.

## Outputs

`data/processed/<kind>/<snapshot>/…` (rebuildable), `data/raw/overtime/` (commit — irreplaceable
market snapshots), `data/reference/stadium_locations.json` (commit), `data/ncaaf_clv.db` (durable store;
`v_game_lines` joins Overtime lines to the ESPN game and DraftKings reference, `v_game_weather` the latest
kickoff-window weather per game).

## Depends on / feeds

Depends on: `betting-data-normalizing`; shares the AccuWeather pattern with `mlb-weather-reporting`. Feeds: `canonical-team-resolver` (register `game_map`),
`market-signal-detecting` (line history), `xgboost-sports-betting` (NCAAF features).
