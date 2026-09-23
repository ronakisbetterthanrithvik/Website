"""SportsGameOdds (v2) adapter — primary source for live props and game lines.

Billing is per object: each event returned counts once, no matter how many markets
it carries. So one request for a week's games with all five prop markets plus game
lines costs about one object per game.

Response shape (from SGO docs; confirm against the first saved raw file):
  data[] = {
    eventID, leagueID,
    teams: {home: {teamID, names: {long}}, away: {...}},
    status: {startsAt, started, completed, cancelled},
    players: {playerID: {name | firstName/lastName, teamID}},
    odds: {oddID: {statID, statEntityID, periodID, betTypeID, sideID, playerID,
                   byBookmaker: {bookID: {odds, overUnder, spread, available, lastUpdatedAt}}}}
  }
  oddID = {statID}-{statEntityID}-{periodID}-{betTypeID}-{sideID}
"""

import re
from datetime import datetime

import httpx

from stackd.data import teams
from stackd.odds.base import OddsRow
from stackd.odds.prices import american_to_decimal, parse_american

BASE_URL = "https://api.sportsgameodds.com/v2"
PROVIDER = "sgo"

PROP_STATS = {
    "passing_yards": "pass_yds",
    "rushing_yards": "rush_yds",
    "receiving_yards": "rec_yds",
    "receiving_receptions": "receptions",
}

# SGO bookmakerID -> canonical book key.
BOOK_MAP = {
    "draftkings": "draftkings",
    "fanduel": "fanduel",
    "betmgm": "betmgm",
    "caesars": "caesars",
    "williamhill": "caesars",
    "pinnacle": "pinnacle",
    "espnbet": "espnbet",
    "betrivers": "betrivers",
    "fanatics": "fanatics",
}


def odd_ids() -> list[str]:
    ids: list[str] = []
    for stat in PROP_STATS:
        ids += [f"{stat}-PLAYER_ID-game-ou-over", f"{stat}-PLAYER_ID-game-ou-under"]
    # Anytime TD: SGO lists it as a yes/no market; the ou 0.5 form is harmless to request too.
    ids += [
        "touchdowns-PLAYER_ID-game-yn-yes",
        "touchdowns-PLAYER_ID-game-yn-no",
        "touchdowns-PLAYER_ID-game-ou-over",
        "touchdowns-PLAYER_ID-game-ou-under",
    ]
    ids += [
        "points-home-game-ml-home",
        "points-away-game-ml-away",
        "points-home-game-sp-home",
        "points-away-game-sp-away",
        "points-all-game-ou-over",
        "points-all-game-ou-under",
    ]
    return ids


class SGOClient:
    def __init__(self, api_key: str, transport: httpx.BaseTransport | None = None, timeout: float = 30):
        if not api_key:
            raise ValueError("SGO_API_KEY is not set in stackd/.env")
        self._http = httpx.Client(
            base_url=BASE_URL, headers={"x-api-key": api_key}, timeout=timeout, transport=transport
        )

    def events(
        self,
        starts_after: datetime | None = None,
        starts_before: datetime | None = None,
        event_ids: list[str] | None = None,
        include_odds: bool = True,
        max_pages: int = 5,
    ) -> tuple[list[dict], list[tuple[dict, int, dict, int]]]:
        """Returns (events, calls) where calls = [(params, status, headers, objects)] for usage logging."""
        params: dict = {"leagueID": "NFL", "limit": 100}
        if include_odds:
            params["oddIDs"] = ",".join(odd_ids())
            params["oddsAvailable"] = "true"
        if starts_after:
            params["startsAfter"] = starts_after.isoformat()
        if starts_before:
            params["startsBefore"] = starts_before.isoformat()
        if event_ids:
            params["eventIDs"] = ",".join(event_ids)

        events: list[dict] = []
        calls: list[tuple[dict, int, dict, int]] = []
        cursor = None
        for _ in range(max_pages):
            page_params = {**params, **({"cursor": cursor} if cursor else {})}
            resp = self._http.get("/events", params=page_params)
            if resp.is_error:
                calls.append((page_params, resp.status_code, dict(resp.headers), 0))
                resp.raise_for_status()
            body = resp.json()
            page = body.get("data") or []
            calls.append((page_params, resp.status_code, dict(resp.headers), len(page)))
            events.extend(page)
            cursor = body.get("nextCursor")
            if not cursor or not page:
                break
        return events, calls

    def account_usage(self) -> tuple[dict, int, dict]:
        resp = self._http.get("/account/usage")
        resp.raise_for_status()
        return resp.json(), resp.status_code, dict(resp.headers)


def _player_name(event: dict, player_id: str | None) -> tuple[str | None, str | None]:
    if not player_id:
        return None, None
    info = (event.get("players") or {}).get(player_id) or {}
    name = info.get("name") or " ".join(
        p for p in (info.get("firstName"), info.get("lastName")) if p
    )
    if not name:
        # "JAMES_COOK_1_NFL" -> "James Cook"
        name = re.sub(r"_\d+_NFL$", "", player_id).replace("_", " ").title()
    return name, teams.to_abbr(info.get("teamID"))


def _team(event: dict, side: str) -> str | None:
    t = (event.get("teams") or {}).get(side) or {}
    names = t.get("names") or {}
    return teams.to_abbr(t.get("teamID")) or teams.to_abbr(names.get("long"))


def _classify(odd: dict, odd_id: str) -> tuple[str, str] | None:
    """Return (canonical market, side) or None if we don't track this odd."""
    parts = odd_id.split("-")
    stat = odd.get("statID") or (parts[0] if parts else None)
    entity = odd.get("statEntityID") or (parts[1] if len(parts) > 1 else None)
    period = odd.get("periodID") or (parts[2] if len(parts) > 2 else None)
    bet = odd.get("betTypeID") or (parts[3] if len(parts) > 3 else None)
    side = odd.get("sideID") or (parts[4] if len(parts) > 4 else None)
    if period not in ("game", None):
        return None
    if stat in PROP_STATS and bet == "ou" and side in ("over", "under"):
        return PROP_STATS[stat], side
    if stat == "touchdowns" and entity not in ("home", "away", "all"):
        if bet == "yn" and side in ("yes", "no"):
            return "anytime_td", side
        if bet == "ou" and side in ("over", "under"):
            return "anytime_td", "yes" if side == "over" else "no"
    if stat == "points":
        if bet == "ml" and side in ("home", "away"):
            return "h2h", side
        if bet == "sp" and side in ("home", "away"):
            return "spread", side
        if bet == "ou" and entity == "all" and side in ("over", "under"):
            return "total", side
    return None


def parse_events(
    events: list[dict], keep_books: set[str], reference_books: set[str], fetched_at: str
) -> tuple[list[OddsRow], dict]:
    rows: list[OddsRow] = []
    stats = {"events": len(events), "odds_seen": 0, "odds_kept": 0, "unknown_books": set()}
    for event in events:
        event_id = event.get("eventID")
        home, away = _team(event, "home"), _team(event, "away")
        start = (event.get("status") or {}).get("startsAt")
        for odd_id, odd in (event.get("odds") or {}).items():
            stats["odds_seen"] += 1
            kind = _classify(odd, odd_id)
            if not kind:
                continue
            market, side = kind
            player_id = odd.get("playerID") if market not in ("h2h", "spread", "total") else None
            if market not in ("h2h", "spread", "total") and not player_id:
                entity = odd.get("statEntityID") or odd_id.split("-")[1]
                player_id = entity if entity not in ("home", "away", "all") else None
            name, player_team = _player_name(event, player_id)
            for book_id, quote in (odd.get("byBookmaker") or {}).items():
                book = BOOK_MAP.get(book_id)
                if book is None:
                    stats["unknown_books"].add(book_id)
                    continue
                if book not in keep_books and book not in reference_books:
                    continue
                if quote.get("available") is False:
                    continue
                american = parse_american(quote.get("odds"))
                if american is None:
                    continue
                line = None
                if market == "spread":
                    line = _float(quote.get("spread"))
                elif market != "h2h":
                    line = _float(quote.get("overUnder"))
                if market == "anytime_td":
                    line = None
                rows.append(
                    OddsRow(
                        provider=PROVIDER,
                        event_id=event_id,
                        commence_time=start,
                        home_team=home,
                        away_team=away,
                        market=market,
                        book=book,
                        side=side,
                        price_american=american,
                        price_decimal=round(american_to_decimal(american), 4),
                        line=line,
                        player_name=name,
                        provider_player_id=player_id,
                        player_team=player_team,
                        last_update=quote.get("lastUpdatedAt"),
                        fetched_at=fetched_at,
                        is_reference=book in reference_books,
                    )
                )
                stats["odds_kept"] += 1
    stats["unknown_books"] = sorted(stats["unknown_books"])
    return rows, stats


def _float(value) -> float | None:
    try:
        return float(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None
