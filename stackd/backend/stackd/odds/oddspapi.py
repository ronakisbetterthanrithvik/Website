"""OddsPapi (v4) adapter — closing lines (historical snapshots) and a sharp reference (Pinnacle).

Billing is per request (free tier ~250/month), so every method here is one request and
the service layer caches aggressively. Market ids are numeric and there is one id per
handicap, so we map them to our canonical markets through the `/markets` catalog.

Response shapes (from OddsPapi docs/blog; confirm against the first saved raw file):
  /odds            -> {fixtureId, bookmakerOdds: {slug: {markets: {marketId: {outcomes: {outcomeId:
                        {players: {playerId: {active, price (decimal), playerName, changedAt}}}}}}}}}
  /historical-odds -> {bookmakers: {slug: {markets: {...: {outcomes: {...: {players: {playerId:
                        [ {createdAt, price, active?}, ... ]}}}}}}}}   (max 3 bookmakers per call)
Game lines live under player key "0".
"""

import re
import time
from datetime import datetime

import httpx

from stackd.data import teams
from stackd.odds.base import OddsRow
from stackd.odds.prices import decimal_to_american

BASE_URL = "https://api.oddspapi.io/v4"
PROVIDER = "oddspapi"
NFL_SPORT_ID = 14

BOOK_MAP = {
    "draftkings": "draftkings",
    "fanduel": "fanduel",
    "betmgm": "betmgm",
    "caesars": "caesars",
    "williamhill": "caesars",
    "pinnacle": "pinnacle",
}

_PERIOD_WORDS = re.compile(r"\b(1st|2nd|3rd|4th|first|second|half|quarter|q[1-4]|h[12]|longest|alt)\b", re.I)


def _first(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


class OddsPapiClient:
    MIN_GAP_SECONDS = 1.0  # docs mention ~0.88s cooldown per endpoint

    def __init__(self, api_key: str, transport: httpx.BaseTransport | None = None, timeout: float = 30):
        if not api_key:
            raise ValueError("ODDSPAPI_API_KEY is not set in stackd/.env")
        self._key = api_key
        self._http = httpx.Client(base_url=BASE_URL, timeout=timeout, transport=transport)
        self._last_call: dict[str, float] = {}

    def get(self, endpoint: str, **params) -> tuple[object, dict, int, dict]:
        """One request. Returns (json, params_without_key, status, headers)."""
        wait = self.MIN_GAP_SECONDS - (time.monotonic() - self._last_call.get(endpoint, 0))
        if wait > 0:
            time.sleep(wait)
        resp = self._http.get(endpoint, params={**params, "apiKey": self._key})
        self._last_call[endpoint] = time.monotonic()
        resp.raise_for_status()
        return resp.json(), params, resp.status_code, dict(resp.headers)


# ---- catalog / fixtures ---------------------------------------------------------------

def find_nfl_tournament(tournaments: list[dict]) -> str | None:
    for t in tournaments or []:
        name = str(_first(t, "tournamentName", "name", default="")).strip().lower()
        if name in ("nfl", "national football league"):
            return str(_first(t, "tournamentId", "id"))
    return None


def classify_market(entry: dict) -> str | None:
    name = str(_first(entry, "marketName", "name", "marketNameShort", default="")).lower()
    if not name or _PERIOD_WORDS.search(name):
        return None
    if "touchdown" in name and ("anytime" in name or "to score" in name):
        return "anytime_td"
    if "passing yards" in name:
        return "pass_yds"
    if "rushing yards" in name:
        return "rush_yds"
    if "receiving yards" in name:
        return "rec_yds"
    if "receptions" in name:
        return "receptions"
    if "moneyline" in name or name in ("winner", "match winner", "1x2 (incl. ot)", "winner (incl. overtime)"):
        return "h2h"
    if "spread" in name or "handicap" in name:
        return "spread"
    if "total" in name or "over/under" in name:
        return "total"
    return None


def build_market_index(catalog: list[dict]) -> dict[str, dict]:
    """marketId -> {market, handicap, outcomes: {outcomeId: outcomeName}}"""
    index: dict[str, dict] = {}
    for entry in catalog or []:
        market = classify_market(entry)
        if not market:
            continue
        outcomes = {
            str(_first(o, "outcomeId", "id")): str(_first(o, "outcomeName", "name", default=""))
            for o in entry.get("outcomes") or []
        }
        index[str(_first(entry, "marketId", "id"))] = {
            "market": market,
            "handicap": _to_float(_first(entry, "handicap", "line")),
            "outcomes": outcomes,
        }
    return index


def parse_fixture(fx: dict) -> dict:
    p1 = _first(fx, "participant1Name", "homeTeamName", "participant1")
    p2 = _first(fx, "participant2Name", "awayTeamName", "participant2")
    return {
        "fixture_id": str(_first(fx, "fixtureId", "id")),
        "p1": teams.to_abbr(p1 if isinstance(p1, str) else _first(p1 or {}, "name")),
        "p2": teams.to_abbr(p2 if isinstance(p2, str) else _first(p2 or {}, "name")),
        "start_time": _first(fx, "startTime", "startsAt", "start"),
        "status_id": _first(fx, "statusId", "status"),
        "has_odds": _first(fx, "hasOdds"),
    }


# ---- odds parsing -----------------------------------------------------------------------

def _side(market: str, outcome_name: str, p1: str | None, p2: str | None, home: str | None) -> str | None:
    name = outcome_name.strip().lower()
    if name in ("over", "under", "yes", "no"):
        if market == "anytime_td":
            return {"over": "yes", "under": "no"}.get(name, name)
        return name
    if market in ("h2h", "spread"):
        team = {"1": p1, "2": p2}.get(name) or teams.to_abbr(outcome_name)
        if team and home:
            return "home" if team == home else "away"
    return None


def iter_prices(book_markets: dict, snapshots: bool):
    """Yield (marketId, outcomeId, playerId, node) from a bookmaker's markets dict."""
    for market_id, market in (book_markets or {}).items():
        for outcome_id, outcome in (market.get("outcomes") or {}).items():
            for player_id, node in (outcome.get("players") or {}).items():
                yield str(market_id), str(outcome_id), str(player_id), node


def parse_odds(
    payload: dict,
    event_id: str,
    market_index: dict[str, dict],
    fixture: dict,
    home: str | None,
    away: str | None,
    commence_time: str | None,
    keep_books: set[str],
    reference_books: set[str],
    fetched_at: str,
    closing_before: datetime | None = None,
) -> tuple[list[OddsRow], dict]:
    """Parse /odds (live) or /historical-odds (when closing_before is set: last snapshot before kickoff)."""
    historical = closing_before is not None
    books = payload.get("bookmakers" if historical else "bookmakerOdds") or {}
    if historical and not books and len(payload) == 1:
        # response may be keyed by fixtureId first
        books = next(iter(payload.values())).get("bookmakers") or {}
    rows: list[OddsRow] = []
    stats = {"markets_seen": 0, "unmapped_markets": set(), "rows": 0}
    for slug, book_data in books.items():
        book = BOOK_MAP.get(slug)
        if not book or (book not in keep_books and book not in reference_books):
            continue
        for market_id, outcome_id, player_id, node in iter_prices(book_data.get("markets"), historical):
            stats["markets_seen"] += 1
            meta = market_index.get(market_id)
            if not meta:
                stats["unmapped_markets"].add(market_id)
                continue
            market = meta["market"]
            is_prop = market not in ("h2h", "spread", "total")
            if is_prop == (player_id == "0"):
                continue
            quote = _closing(node, closing_before) if historical else node
            if not quote or quote.get("active") is False:
                continue
            price = _to_float(quote.get("price"))
            if not price or price <= 1:
                continue
            side = _side(market, meta["outcomes"].get(outcome_id, ""), fixture.get("p1"), fixture.get("p2"), home)
            if not side:
                continue
            line = meta["handicap"]
            if market == "spread" and line is not None and fixture.get("p1") and fixture.get("p1") != (home if side == "home" else away):
                line = -line  # catalog handicap is quoted for participant 1
            if market in ("h2h", "anytime_td"):
                line = None
            rows.append(
                OddsRow(
                    provider=PROVIDER,
                    event_id=event_id,
                    commence_time=commence_time,
                    home_team=home,
                    away_team=away,
                    market=market,
                    book=book,
                    side=side,
                    price_american=decimal_to_american(price),
                    price_decimal=round(price, 4),
                    line=line,
                    player_name=_clean_player_name(quote.get("playerName") or node_name(node)) if is_prop else None,
                    provider_player_id=player_id if is_prop else None,
                    last_update=quote.get("changedAt") or quote.get("createdAt"),
                    fetched_at=fetched_at,
                    is_reference=book in reference_books,
                )
            )
            stats["rows"] += 1
    stats["unmapped_markets"] = len(stats["unmapped_markets"])
    return rows, stats


def node_name(node) -> str | None:
    if isinstance(node, list):
        for snap in reversed(node):
            if isinstance(snap, dict) and snap.get("playerName"):
                return snap["playerName"]
    return None


def _closing(node, before: datetime) -> dict | None:
    snaps = node if isinstance(node, list) else [node]
    best = None
    for snap in snaps:
        ts = _parse_ts(snap.get("createdAt") or snap.get("changedAt"))
        if ts and ts < before and (best is None or ts >= best[0]):
            best = (ts, snap)
    return best[1] if best else None


def _clean_player_name(name: str | None) -> str | None:
    return name.strip() if name else None


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _to_float(value) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
