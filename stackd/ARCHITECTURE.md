# Stackd — architecture plan

NFL-only player-prop edge finder and parlay builder. This document is the plan;
no application code exists yet. Each phase ends with a stop so it can be tested.

## Repo layout

The repo root already holds an unrelated Vite/React product page, so Stackd lives
in its own folder and doesn't touch it.

```
stackd/
  ARCHITECTURE.md
  .env.example              ODDS_API_KEY=...   (real .env is gitignored)
  backend/
    pyproject.toml          fastapi, uvicorn, nflreadpy, polars, httpx,
                            rapidfuzz, pydantic-settings, pytest
    stackd/
      config.py             settings from .env (key, data dir, books, TTLs)
      api/                  FastAPI app + routers (data, odds, props, parlays, tracker)
      data/
        nflverse.py         loaders + parquet cache + daily refresh
        odds_api.py         Odds API v4 client, response cache, credit log, 30-min guard
        teams.py            Odds API team names <-> nflverse abbreviations, colors, logos
        names.py            player-name normalizer + matcher + overrides CSV
      model/                (phase 2) features, projections, distributions, edges, reasons
      parlay/               (phase 3) odds math, per-book combining, correlation checks
      tracker/              (phase 5) SQLite store, grading, CLV, aggregates
      cli.py                `python -m stackd.cli ...` for manual refresh / reports
    data/                   gitignored runtime data
      nflverse/*.parquet
      odds/{events,props,lines}/*.json   raw responses, timestamped
      odds_usage.csv        one row per paid request (credits from headers)
      stackd.sqlite         picks, slips, results, closing lines
    overrides/player_name_overrides.csv   (committed) odds_name,team,gsis_id
    tests/                  pytest, uses recorded JSON fixtures — never calls paid APIs
  frontend/                 (phase 4) Vite + React + TS + Tailwind
```

Backend on `localhost:8000`, frontend dev server on `localhost:5173` proxying `/api`.

## Phase 1 — data layer

### nflverse (free)
`nflreadpy` returns Polars DataFrames. Load the current season via
`get_current_season()` / `get_current_week()`:

| dataset | loader | used for |
|---|---|---|
| weekly player stats | `load_player_stats(season, "week")` | usage, yards, receptions, TDs, grading |
| snap counts | `load_snap_counts` | snap share (keyed by PFR id → join through `load_players`) |
| depth charts | `load_depth_charts` | role (WR1/WR2, RB1...) |
| injuries | `load_injuries` | teammate-out adjustments |
| schedules | `load_schedules(season)` | kickoff, spread_line, total_line, results |
| rosters | `load_rosters` | team, position, `headshot_url` |
| play-by-play | `load_pbp` | defense vs. position |
| players / teams | `load_players`, `load_teams` | ID crosswalk, team colors |

Cache: each dataset is written to `data/nflverse/<name>_<season>.parquet` with a
`meta.json` of fetch times. A read returns the cache if it is < 24 h old, otherwise
refreshes. A FastAPI lifespan task refreshes everything once a day (nflverse is free,
so a scheduled loop is fine here). A failed refresh keeps serving the old file.

### The Odds API (paid credits)
Sport `americanfootball_nfl`, `regions=us`, `oddsFormat=american`,
`bookmakers=draftkings,fanduel,betmgm,williamhill_us` (Caesars' key is
`williamhill_us`; more books are one config line).

| call | endpoint | cost |
|---|---|---|
| events list | `GET /v4/sports/americanfootball_nfl/events` | free |
| game lines | `GET /v4/sports/americanfootball_nfl/odds?markets=h2h,spreads,totals` | 3 per call (markets × regions) |
| props, one game | `GET /v4/sports/americanfootball_nfl/events/{id}/odds?markets=player_pass_yds,player_rush_yds,player_receptions,player_reception_yds,player_anytime_td` | up to 5 per game |

A full slate of props is about 16 games × 5 = ~80 credits per refresh. Rules
built into the client:

- **30-minute floor.** Every response is saved with its fetch time. A props or
  lines request inside 30 min of the last one returns the cache and makes no call,
  even with "force".
- **Credit log.** `x-requests-used`, `x-requests-remaining`, `x-requests-last`
  are appended to `odds_usage.csv` and exposed at `/api/odds/usage`.
- **No automatic paid loops.** Paid calls happen only on explicit request (API
  button or CLI). A whole-slate props fetch prints the estimated cost and needs
  `--yes`. No scheduler hits the Odds API without your approval.
- **Budget guard.** If `x-requests-remaining` drops below a configurable floor
  (default 50), paid calls are refused.

Normalized storage: one row per (event, market, book, player, side, line,
price, last_update). Lines are kept per bookmaker. Nothing is averaged at this layer.

### Name matching (`names.py`)
1. **Normalize.** NFKD → ASCII, lowercase, strip punctuation (`.`, `'`, `-`),
   drop suffixes (jr, sr, ii, iii, iv, v), collapse whitespace.
   "D.J. Moore" → "dj moore", "Marvin Harrison Jr." → "marvin harrison".
2. **Overrides CSV first.** `odds_name,team,gsis_id` wins over everything.
3. **Exact normalized match** among players on the two teams in that event
   (from rosters), filtered by the positions the market applies to.
4. **Fuzzy fallback.** `rapidfuzz` `token_sort_ratio` ≥ 90 inside the same
   restricted pool. Ties or low scores are left unmatched rather than guessed.
5. **Unmatched report.** Written to `data/unmatched_players.csv` and
   `/api/data/match-report` so you can add overrides.

### Phase 1 endpoints and CLI (what you'll test)
- `GET /api/health`, `GET /api/data/status` (cache ages, row counts, last refresh)
- `POST /api/data/refresh` (nflverse only)
- `GET /api/events?week=N`: events joined to the nflverse schedule
- `GET /api/odds/lines` and `GET /api/odds/props/{event_id}`: cached-first
- `GET /api/odds/usage`, `GET /api/data/match-report`
- CLI: `refresh-nflverse`, `events`, `fetch-lines`, `fetch-props --event ID`,
  `fetch-props --all --yes`, `usage`, `match-report`
- Tests: normalizer/matcher cases, 30-min guard, header parsing, response parsing
  from recorded fixtures.

## Phase 2 — projection model v1 (explainable)

Features per player-week, all computed from cached parquet:
- **Usage:** weighted blend `0.6 × last-4-games + 0.4 × season` for targets,
  carries, pass attempts, yards per opportunity, catch rate, TD rate.
- **Snap share, target share, carry share**, and depth-chart role.
- **Team implied total** = `total/2 − spread/2` (with nflverse's home-positive
  `spread_line` handled). Volume scales with implied total vs. league average.
- **Opponent vs. position.** From pbp: yards and receptions allowed per game to
  QB/RB/WR/TE over the season, as a ratio to league average, shrunk toward 1.0.
- **Teammate out.** If a teammate with meaningful share is Out/Doubtful, split
  their targets/carries across remaining players by historical share. Where
  there are real games without them, use those splits instead.

Distributions:
- Yardage: Normal(mean, sd). sd comes from the player's game-to-game variation,
  shrunk toward a position-level coefficient of variation.
- Receptions and TDs: Poisson(mean). Anytime TD = `1 − e^(−λ)`.

Edges: for each book, remove the vig from its Over/Under pair to get a fair
probability at that book's line, then edge = model P(side at that line) −
no-vig P. The recommended side is the one with the higher edge. Best book
= best line for that side first (lower for Over, higher for Under), then best odds.
Anytime TD is usually priced "Yes" only, so it uses a de-vig assumption from
the market's typical hold (configurable) and is labelled as approximate.

Reasons: each feature adjustment is logged as a signed contribution to the
mean. The top one or two become template sentences ("WR2 ruled out; target
share 31% without him", "Opponent allows 1.3× league average rush yards to RBs").

## Phase 3 — parlay math
Pure functions with tests: American↔decimal, combined decimal = product,
payout/profit, and 2–8 legs. Combined odds are computed per book using only
books that price every leg, and the best is flagged. Hit chance = product of
leg probabilities (labelled as assuming independence). EV = stake ×
(decimal × hit − 1). Warnings cover same player twice (blocked) and same game,
with a stronger warning for QB + own pass-catcher and for RB rush + team TD
patterns.

## Phase 4 — frontend
Vite + React + TS + Tailwind. Design tokens from the brief go into
`tailwind.config` as named colors. Geist and Geist Mono are self-hosted via
the `geist` npm package. TanStack Query handles fetching and the bet slip lives
in a small Zustand store persisted to localStorage. Components: `TopNav`,
`FilterBar`, `PropCard` (Headshot with initials + team-color fallback,
BookLineRow, HitBar), `BetSlip` (LegMeter, BookPicker, StakeInput,
PayoutLadder), and the tabs Props / Game lines / Parlays / Tracker.

## Phase 5 — tracker
SQLite tables: `recommendations`, `slips`, `slip_legs`, `closing_lines`,
`results`. Grading runs after nflverse stats update for finished games. CLV
compares our saved line/odds to the last pre-kickoff snapshot. That snapshot
needs one paid props call per game near kickoff, so I'll ask before automating
it. Aggregates cover hit rate, ROI, and CLV by week and by prop type, shown as
simple charts on the Tracker tab.

## Guardrails
- `.env` and `stackd/backend/data/` are gitignored. Only `.env.example` is committed.
- Tests and CI never call the Odds API.
- Any loop that spends Odds API credits requires your explicit go-ahead.
