# Stackd backend (Phase 1: data layer)

Python 3.11+, FastAPI, Polars. Plan and design notes: [`../ARCHITECTURE.md`](../ARCHITECTURE.md).

## Setup

```bash
cd stackd/backend
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
cp ../.env.example ../.env      # then fill in SGO_API_KEY and ODDSPAPI_API_KEY
pytest                          # offline; never calls a paid API
```

## What costs money

| Command / endpoint | Cost | Guard |
|---|---|---|
| `refresh-nflverse`, `status`, `events`, `props`, `usage`, `match-report`, every `GET /api/...` | free | reads cache only |
| `sgo-week` / `POST /api/odds/sgo/week` | ~1 SGO object per game (~16) | estimate unless `--yes` / `confirm=true`; 30-min floor |
| `sgo-event ID` / `POST /api/odds/sgo/event/{id}` | 1 SGO object | 30-min floor (also blocked if its week was fetched <30 min ago) |
| `op-fixtures` | 1 OddsPapi request (+1 once, to find the NFL tournament id) | 30-min floor |
| `op-odds FIXTURE` | 1 request (+1 when the market catalog is >7 days old) | 30-min floor |
| `op-closing FIXTURE` | 1 request (max 3 books), only after kickoff | cached permanently |

All paid calls are refused if they would drop the month's remaining allowance below
the floor (`STACKD_SGO_OBJECT_FLOOR`, `STACKD_ODDSPAPI_REQUEST_FLOOR`). Nothing paid
runs on a timer.

## Testing Phase 1

Run CLI commands as `python -m stackd.cli <command>` from `stackd/backend`.

1. **nflverse (free).**
   `refresh-nflverse`, then `status`. All nine datasets should show `ok` with row counts.
   Play-by-play is the slowest.
2. **Week and games (free).** `events` lists this week's games from the nflverse schedule, with
   spread and total. Before any odds fetch, `sgo_event_id` is empty.
3. **SportsGameOdds.**
   - `sgo-week` prints the estimated cost and spends nothing.
   - `sgo-week --yes` fetches it.
   - Run `sgo-week --yes` again: it should say *Skipped … next fetch allowed at …*.
   - `props --market pass_yds` shows lines per book with `gsis_id`.
   - `events` now shows `sgo_event_id` and quote counts.
4. **OddsPapi.**
   - `op-fixtures` maps fixtures to nflverse `game_id`s.
   - `op-odds <fixture_id>` fetches that game's odds, including Pinnacle.
   - After a game has kicked off, `op-closing <fixture_id>` fetches its closing lines.
5. **Spend and matching (free).**
   - `usage` shows month-to-date spend per provider.
   - `match-report` lists player names that didn't match.
   - Add fixes to `overrides/player_name_overrides.csv` (`odds_name,team,gsis_id`).

API: `uvicorn stackd.api.app:app --reload`, then open http://localhost:8000/docs.
Paid POSTs return `409` with an estimate when they need `confirm=true`, and `429` inside the
30-minute window.

## First real calls: check the formats

The SportsGameOdds and OddsPapi response formats were written from their public docs,
because this build environment couldn't reach either API. Every response is saved under
`data/odds/raw/<provider>/`, so after the first real call:
- If `props` comes back empty or `match-report` looks wrong, share one raw file (the API
  key is never saved in it) and the parser can be adjusted in minutes.
- SGO's `stats.unknown_books` in the `sgo-week` output lists book ids we don't map yet.
  Add them to `BOOK_MAP` in `stackd/odds/sgo.py` if you want them.
- OddsPapi's `unmapped_markets` count shows how many market ids the catalog didn't
  classify.
