"""FastAPI app. Reads are always cache-only; paid fetches are explicit POSTs."""

import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from stackd.config import get_settings
from stackd.data.nflverse import DATASETS, NflverseCache
from stackd.odds.base import BudgetError, ConfirmationRequired, TooSoonError
from stackd.odds.service import OddsService

log = logging.getLogger("stackd")


class State:
    nfl: NflverseCache
    odds: OddsService


state = State()


async def _daily_nflverse_refresh():
    """nflverse is free, so a background refresh is fine. Never touches paid odds APIs."""
    while True:
        try:
            await asyncio.to_thread(state.nfl.refresh_stale)
        except Exception:  # keep the loop alive
            log.exception("nflverse background refresh failed")
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    state.nfl = NflverseCache(settings)
    state.odds = OddsService(settings, state.nfl)
    task = asyncio.create_task(_daily_nflverse_refresh())
    yield
    task.cancel()


app = FastAPI(title="Stackd", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"], allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(TooSoonError)
async def _too_soon(_, exc: TooSoonError):
    return JSONResponse(status_code=429, content={"error": "too_soon", "detail": str(exc),
                                                  "next_allowed": exc.next_allowed.isoformat()})


@app.exception_handler(BudgetError)
async def _budget(_, exc: BudgetError):
    return JSONResponse(status_code=402, content={"error": "budget", "detail": str(exc)})


@app.exception_handler(ConfirmationRequired)
async def _confirm(_, exc: ConfirmationRequired):
    return JSONResponse(status_code=409, content={"error": "confirmation_required",
                                                  "detail": "Repeat with confirm=true to spend this", "estimate": exc.estimate})


@app.exception_handler(httpx.HTTPStatusError)
async def _upstream(_, exc: httpx.HTTPStatusError):
    return JSONResponse(status_code=502, content={"error": "upstream", "status": exc.response.status_code,
                                                  "detail": exc.response.text[:500]})


def _records(df):
    return df.to_dicts()


# ---- data ---------------------------------------------------------------------------------
@app.get("/api/health")
def health():
    s = get_settings()
    return {"ok": True, "season": state.nfl.season, "sgo_key": bool(s.sgo_api_key),
            "oddspapi_key": bool(s.oddspapi_api_key)}


@app.get("/api/data/status")
def data_status():
    return {"season": state.nfl.season, "datasets": state.nfl.status()}


@app.post("/api/data/refresh")
def data_refresh(dataset: str | None = None, force: bool = True):
    if dataset:
        if dataset not in DATASETS:
            raise HTTPException(404, f"Unknown dataset {dataset}")
        return [state.nfl.refresh(dataset)]
    return state.nfl.refresh_stale(force=force)


@app.get("/api/data/match-report")
def match_report(unmatched_only: bool = False):
    rows = state.odds.match_report()
    if unmatched_only:
        rows = [r for r in rows if r["method"] in ("unmatched", "ambiguous")]
    return rows


@app.get("/api/week/current")
def current_week():
    return {"season": state.nfl.season, "week": state.nfl.current_week()}


# ---- odds reads (cache only) ------------------------------------------------------------------
@app.get("/api/events")
def events(week: int | None = None):
    week = week or state.nfl.current_week()
    return {"week": week, "events": state.odds.events(week)}


@app.get("/api/odds/props")
def props(week: int | None = None, event_id: str | None = None, market: str | None = None,
          book: str | None = None, all_lines: bool = False, provider: str = "sgo"):
    return _records(state.odds.odds("props", week, event_id, market, book, not all_lines, provider))


@app.get("/api/odds/lines")
def lines(week: int | None = None, event_id: str | None = None, provider: str = "sgo"):
    return _records(state.odds.odds("lines", week, event_id, provider=provider))


@app.get("/api/odds/closing")
def closing(event_id: str | None = None):
    df = state.odds.table("oddspapi_closing")
    if event_id:
        df = df.filter((df["event_id"] == event_id) | (df["game_id"] == event_id))
    return _records(df)


@app.get("/api/odds/usage")
def usage():
    return state.odds.usage_summary()


# ---- paid fetches (explicit) ---------------------------------------------------------------
@app.post("/api/odds/sgo/week")
def sgo_week(week: int | None = None, confirm: bool = Query(False)):
    return state.odds.refresh_sgo_week(week or state.nfl.current_week(), confirm=confirm)


@app.post("/api/odds/sgo/event/{event_id}")
def sgo_event(event_id: str):
    return state.odds.refresh_sgo_event(event_id)


@app.post("/api/odds/oddspapi/fixtures")
def op_fixtures(week: int | None = None):
    return state.odds.op_fixtures(week or state.nfl.current_week())


@app.post("/api/odds/oddspapi/odds/{fixture_id}")
def op_odds(fixture_id: str):
    return state.odds.op_odds(fixture_id)


@app.post("/api/odds/oddspapi/closing/{fixture_id}")
def op_closing(fixture_id: str, books: str | None = None):
    return state.odds.op_closing(fixture_id, books.split(",") if books else None)
