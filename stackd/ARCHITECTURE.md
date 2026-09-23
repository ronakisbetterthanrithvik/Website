# Stackd — architecture plan

NFL-only player-prop edge finder and parlay builder. Each phase ends with a stop so
it can be tested. Status: **Phase 1 built** (see `backend/README.md`).

## Repo layout

The repo root already holds an unrelated Vite/React product page, so Stackd lives
in its own folder and doesn't touch it.

```
stackd/
  ARCHITECTURE.md
  .env.example              SGO_API_KEY, ODDSPAPI_API_KEY   (real .env is gitignored)
  backend/
    pyproject.toml          fastapi, uvicorn, nflreadpy, polars, httpx,
                            rapidfuzz, pydantic-settings, pytest
    stackd/
      config.py             settings from .env (keys, data dir, books, budgets, TTLs)
      api/app.py            FastAPI app (reads are cache-only; paid fetches are POSTs)
      data/
        nflverse.py         loaders + parquet cache + daily refresh
        teams.py            provider team names/ids <-> nflverse abbreviations, colors
        names.py            player-name normalizer + matcher + overrides CSV
      odds/
        base.py             OddsRow, usage log, fetch log, spend guard
        prices.py           American <-> decimal
        sgo.py              SportsGameOdds v2 adapter (live props + game lines)
        oddspapi.py         OddsPapi v4 adapter (closing lines, Pinnacle reference)
        service.py          providers -> normalized rows -> game/player ids -> parquet
      model/                (phase 2) features, projections, distributions, edges, reasons
      parlay/               (phase 3) odds math, per-book combining, correlation checks
      tracker/              (phase 5) SQLite store, grading, CLV, aggregates
      cli.py                `python -m stackd.cli ...` for manual refresh / reports
    data/                   gitignored runtime data
      nflverse/*.parquet
      odds/raw/<provider>/*.json         every raw response, timestamped
      odds/normalized/*.parquet          sgo_latest, oddspapi_latest, oddspapi_closing
      odds/usage.csv        one row per paid request (cost + any rate-limit headers)
      odds/fetch_log.json   last fetch per cache key (drives the 30-min floor)
      odds/match_report.csv player-name matching results
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

### Odds providers (paid), behind one provider layer
Every provider's response is converted into the same `OddsRow`: provider, event,
kickoff, home/away (nflverse abbreviations), market, book, side, line, American and
decimal price, player name/id, last update. Everything downstream (model, parlays, UI,
tracker) reads only these rows, so a provider can be swapped by adding an adapter.

Canonical markets: `pass_yds`, `rush_yds`, `receptions`, `rec_yds`, `anytime_td`,
`h2h`, `spread`, `total`. Canonical books: `draftkings`, `fanduel`, `betmgm`,
`caesars` (the four we recommend), plus `pinnacle` stored as a sharp reference only.

**SportsGameOdds (primary: live props and game lines).** v2 `/events` with
`leagueID=NFL` and an `oddIDs` filter for the five prop markets and the three game
lines. Billed per object (one event is one object), so a whole week of props and
lines is one request costing about one object per game (~16). Free tier is 1,000
objects/month.

**OddsPapi (closing lines and Pinnacle).** v4 `/fixtures`, `/markets` (catalog that
maps numeric market ids to our markets; one id per handicap), `/odds`, and
`/historical-odds` (every snapshot; max 3 books per call). Billed per request, free
tier ~250/month. The closing line is the last snapshot before kickoff.

**Spend rules, enforced in code:**
- **30-minute floor** per cache key (week, event, fixture). Inside it the cache is
  served and nothing is spent. Fetching a week also marks each of its events.
- **Usage log.** Every paid request is appended to `odds/usage.csv` with its cost
  and any rate-limit headers. Month-to-date spend is computed from this log.
- **Budget floor.** A call is refused if it would take the month's remaining
  allowance below a configurable floor (SGO 100 objects, OddsPapi 25 requests).
- **Confirmation.** Anything costing more than one unit (an SGO week) returns a
  cost estimate unless called with `confirm=true` / `--yes`.
- **No paid loops.** Nothing on a timer calls SGO or OddsPapi.

**Main lines.** When a book lists several lines for one player and market, the main
one is the line whose Over/Under prices are closest to even. The rest stay stored
as alternates.

### Name matching (`names.py`)
1. **Normalize.** NFKD → ASCII, lowercase, strip punctuation (`.`, `'`, `-`),
   drop suffixes (jr, sr, ii, iii, iv, v), flip "Last, First" (OddsPapi's format).
   "D.J. Moore" → "dj moore", "Mahomes, Patrick" → "patrick mahomes".
2. **Overrides CSV first.** `odds_name,team,gsis_id` wins over everything.
3. **Exact normalized match** among players on the two teams in that event
   (from rosters), filtered by the positions the market applies to.
4. **Fuzzy fallback.** `rapidfuzz` `token_sort_ratio` ≥ 90 inside the same
   restricted pool. Ties or low scores are left unmatched rather than guessed.
5. **Unmatched report.** Written to `data/unmatched_players.csv` and
   `/api/data/match-report` so you can add overrides.

### Phase 1 endpoints and CLI
See `backend/README.md` for the full list and a test walkthrough.

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
no-vig P. Pinnacle's no-vig price is shown alongside as a sharp-market check.
The recommended side is the one with the higher edge. Best book
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
compares our saved line/odds to the closing line from OddsPapi's historical
snapshots, fetched after kickoff (one request per game, max 3 books). A full week
is ~16 requests, so I'll ask before automating it. Aggregates cover hit rate, ROI, and CLV by week and by prop type, shown as
simple charts on the Tracker tab.

## Guardrails
- `.env` and `stackd/backend/data/` are gitignored. Only `.env.example` is committed.
- Tests and CI never call SportsGameOdds or OddsPapi.
- Any loop that spends paid API credits requires your explicit go-ahead.
