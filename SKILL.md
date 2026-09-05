---
name: ncaaf-clv
description: NCAAF handicapping data pipeline — collect ESPN FBS scoreboard, per-game Gamecast/Box Score/Play-by-Play and per-team Statistics/Schedule/Roster as SQLite-ready JSON; normalise Overtime.ag COLLEGE FB Game / 1st Half / 1st Quarter odds; join odds to ESPN games by a deterministic team-pair key; validate games, teams and odds; load everything into SQLite for closing-line-value analysis. Use whenever a task involves college football (NCAAF/FBS) game data, team pages, Overtime.ag college football lines, joining sportsbook lines to ESPN ids, or CLV tracking for college football.
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
- Backfill snapshots are `backfill-<year>`, one per season, with that season's team dimension
  (conference alignment as of the year). `espn_odds.close_*` is NULL unless ESPN exposed a real
  close block (ESPN BET 2022+, open blocks 2023+); `home_spread` / `total` are the last recorded line.
- Send no custom User-Agent to ESPN: the edge 403s bare product tokens (`ncaaf-clv/0.1`).

## Outputs

`data/processed/<kind>/<snapshot>/…` (rebuildable), `data/raw/overtime/` (commit — irreplaceable
market snapshots), `data/ncaaf_clv.db` (durable store; `v_game_lines` view joins Overtime lines
to the ESPN game and DraftKings reference).

## Depends on / feeds

Depends on: `betting-data-normalizing`. Feeds: `canonical-team-resolver` (register `game_map`),
`market-signal-detecting` (line history), `xgboost-sports-betting` (NCAAF features).
