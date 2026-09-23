"""Orchestrates providers → normalized rows → nflverse ids → parquet.

Spend rules (see ARCHITECTURE.md):
  * Nothing here runs on a timer. Paid calls only happen when a CLI command or API
    endpoint asks for them.
  * Every cache key has a 30-minute floor. Inside it, callers get the cache.
  * A call that costs more than one unit (e.g. a whole week from SGO) needs confirm=True;
    without it the caller gets a cost estimate instead.
"""

import csv
import json
import logging
from datetime import UTC, datetime, timedelta

import polars as pl

from stackd.config import Settings, get_settings
from stackd.data.names import NameMatcher, load_overrides
from stackd.data.nflverse import NflverseCache
from stackd.odds import oddspapi, sgo
from stackd.odds.base import (
    PROP_MARKETS,
    ConfirmationRequired,
    FetchLog,
    OddsRow,
    PaidCallGuard,
    UsageLog,
    save_raw,
)

log = logging.getLogger(__name__)

ROW_SCHEMA = {
    "provider": pl.Utf8, "event_id": pl.Utf8, "commence_time": pl.Utf8,
    "home_team": pl.Utf8, "away_team": pl.Utf8, "market": pl.Utf8, "book": pl.Utf8,
    "side": pl.Utf8, "price_american": pl.Int64, "price_decimal": pl.Float64, "line": pl.Float64,
    "player_name": pl.Utf8, "provider_player_id": pl.Utf8, "player_team": pl.Utf8,
    "last_update": pl.Utf8, "fetched_at": pl.Utf8, "is_reference": pl.Boolean,
    "game_id": pl.Utf8, "season": pl.Int64, "week": pl.Int64,
    "provider_main": pl.Boolean,
    "gsis_id": pl.Utf8, "match_method": pl.Utf8, "match_score": pl.Float64, "is_main": pl.Boolean,
}

_REPORT_FIELDS = ["provider", "odds_name", "teams", "team_hint", "market", "method", "score", "gsis_id", "matched_name", "seen_at"]


class OddsService:
    def __init__(self, settings: Settings | None = None, nfl: NflverseCache | None = None,
                 sgo_client=None, oddspapi_client=None):
        self.settings = settings or get_settings()
        self.nfl = nfl or NflverseCache(self.settings)
        self.dir = self.settings.odds_dir
        (self.dir / "normalized").mkdir(parents=True, exist_ok=True)
        self.usage = UsageLog(self.dir / "usage.csv")
        self.fetches = FetchLog(self.dir / "fetch_log.json")
        interval = timedelta(minutes=self.settings.odds_min_refresh_minutes)
        self.sgo_guard = PaidCallGuard("sgo", self.usage, self.fetches,
                                       self.settings.sgo_monthly_objects, self.settings.sgo_object_floor, interval)
        self.op_guard = PaidCallGuard("oddspapi", self.usage, self.fetches,
                                      self.settings.oddspapi_monthly_requests, self.settings.oddspapi_request_floor, interval)
        self._sgo = sgo_client
        self._op = oddspapi_client
        self._matcher: NameMatcher | None = None

    # ---- lazily-built clients / matcher ------------------------------------------------
    @property
    def sgo(self) -> sgo.SGOClient:
        if self._sgo is None:
            self._sgo = sgo.SGOClient(self.settings.sgo_api_key)
        return self._sgo

    @property
    def op(self) -> oddspapi.OddsPapiClient:
        if self._op is None:
            self._op = oddspapi.OddsPapiClient(self.settings.oddspapi_api_key)
        return self._op

    @property
    def matcher(self) -> NameMatcher:
        if self._matcher is None:
            self._matcher = NameMatcher(
                self.nfl.roster_candidates(),
                load_overrides(self.settings.overrides_csv),
                self.settings.fuzzy_match_threshold,
            )
        return self._matcher

    @property
    def keep_books(self) -> set[str]:
        return set(self.settings.books)

    @property
    def reference_books(self) -> set[str]:
        return set(self.settings.reference_books)

    # ---- SportsGameOdds -------------------------------------------------------------------
    def _week_window(self, week: int) -> tuple[datetime, datetime, int]:
        games = self.nfl.week_games(week)
        if games.height == 0:
            raise ValueError(f"No nflverse games found for week {week}")
        first = datetime.fromisoformat(str(games["gameday"].min())).replace(tzinfo=UTC)
        last = datetime.fromisoformat(str(games["gameday"].max())).replace(tzinfo=UTC)
        return first - timedelta(hours=12), last + timedelta(days=1, hours=12), games.height

    def sgo_week_key(self, week: int) -> str:
        return f"sgo:week:{self.nfl.season}:{week}"

    def refresh_sgo_week(self, week: int, confirm: bool = False) -> dict:
        start, end, n_games = self._week_window(week)
        self.sgo_guard.check_interval([self.sgo_week_key(week)])
        self.sgo_guard.check_budget(n_games)
        estimate = self.sgo_guard.estimate(n_games, "objects", {"week": week, "games": n_games,
                                                                 "window": [start.isoformat(), end.isoformat()]})
        if not confirm:
            raise ConfirmationRequired(estimate)
        events, calls = self.sgo.events(starts_after=start, starts_before=end)
        return self._ingest_sgo(events, calls, label=f"week{week}",
                                keys=[self.sgo_week_key(week)])

    def refresh_sgo_event(self, event_id: str) -> dict:
        key = f"sgo:event:{event_id}"
        self.sgo_guard.check_interval([key])
        self.sgo_guard.check_budget(1)
        events, calls = self.sgo.events(event_ids=[event_id])
        return self._ingest_sgo(events, calls, label=f"event_{event_id}", keys=[key])

    def _ingest_sgo(self, events: list[dict], calls: list, label: str, keys: list[str]) -> dict:
        for params, status, headers, n in calls:
            self.usage.record("sgo", "/events", params, n, "objects", status, headers)
        fetched_at = datetime.now(UTC).isoformat()
        raw_path = save_raw(self.dir, "sgo", label, events)
        rows, stats = sgo.parse_events(events, self.keep_books, self.reference_books, fetched_at)
        df = self._enrich(rows)
        self._merge("sgo_latest", df, [e.get("eventID") for e in events])
        self.fetches.mark(*keys, *[f"sgo:event:{e.get('eventID')}" for e in events])
        return {"provider": "sgo", "raw_file": str(raw_path), "rows": df.height, **stats,
                "objects_used": sum(c[3] for c in calls)}

    def sgo_account_usage(self) -> dict:
        body, status, headers = self.sgo.account_usage()
        return body

    # ---- OddsPapi -----------------------------------------------------------------------------
    @property
    def _op_meta_path(self):
        return self.dir / "oddspapi_meta.json"

    def _op_meta(self) -> dict:
        return json.loads(self._op_meta_path.read_text()) if self._op_meta_path.exists() else {}

    def _op_save_meta(self, meta: dict) -> None:
        self._op_meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))

    def _op_call(self, endpoint: str, key: str | None, min_interval: timedelta | None = None, **params):
        if key:
            self.op_guard.check_interval([key], min_interval)
        self.op_guard.check_budget(1)
        body, logged_params, status, headers = self.op.get(endpoint, **params)
        self.usage.record("oddspapi", endpoint, logged_params, 1, "requests", status, headers)
        if key:
            self.fetches.mark(key)
        return body

    def op_tournament_id(self) -> str:
        if self.settings.oddspapi_nfl_tournament_id:
            return self.settings.oddspapi_nfl_tournament_id
        meta = self._op_meta()
        if meta.get("nfl_tournament_id"):
            return meta["nfl_tournament_id"]
        body = self._op_call("/tournaments", "oddspapi:tournaments", sportId=oddspapi.NFL_SPORT_ID)
        save_raw(self.dir, "oddspapi", "tournaments", body)
        tid = oddspapi.find_nfl_tournament(body if isinstance(body, list) else body.get("data", []))
        if not tid:
            raise RuntimeError("Could not find NFL in OddsPapi tournaments; set STACKD_ODDSPAPI_NFL_TOURNAMENT_ID")
        meta["nfl_tournament_id"] = tid
        self._op_save_meta(meta)
        return tid

    def op_market_index(self, refresh: bool = False) -> dict[str, dict]:
        path = self.dir / "oddspapi_markets.json"
        if path.exists() and not refresh:
            age = datetime.now(UTC) - datetime.fromtimestamp(path.stat().st_mtime, UTC)
            if age < timedelta(days=7):
                return oddspapi.build_market_index(json.loads(path.read_text()))
        body = self._op_call("/markets", "oddspapi:markets", timedelta(hours=12), sportId=oddspapi.NFL_SPORT_ID)
        catalog = body if isinstance(body, list) else body.get("data", [])
        path.write_text(json.dumps(catalog))
        return oddspapi.build_market_index(catalog)

    def op_fixtures(self, week: int) -> list[dict]:
        start, end, _ = self._week_window(week)
        body = self._op_call(
            "/fixtures", f"oddspapi:fixtures:{self.nfl.season}:{week}",
            tournamentId=self.op_tournament_id(), **{"from": start.isoformat(), "to": end.isoformat()},
        )
        save_raw(self.dir, "oddspapi", f"fixtures_week{week}", body)
        fixtures = [oddspapi.parse_fixture(f) for f in (body if isinstance(body, list) else body.get("data", []))]
        meta = self._op_meta()
        known = meta.setdefault("fixtures", {})
        for fx in fixtures:
            game = self._game_for(fx["p1"], fx["p2"], fx["start_time"])
            fx.update(game or {})
            known[fx["fixture_id"]] = fx
        self._op_save_meta(meta)
        return fixtures

    def _fixture(self, fixture_id: str) -> dict:
        fx = self._op_meta().get("fixtures", {}).get(str(fixture_id))
        if not fx:
            raise ValueError(f"Unknown fixture {fixture_id}; run fixtures for its week first")
        return fx

    def op_odds(self, fixture_id: str) -> dict:
        fx = self._fixture(fixture_id)
        index = self.op_market_index()
        books = ",".join(sorted(self.keep_books | self.reference_books))
        body = self._op_call("/odds", f"oddspapi:odds:{fixture_id}", fixtureId=fixture_id, bookmakers=books)
        raw = save_raw(self.dir, "oddspapi", f"odds_{fixture_id}", body)
        rows, stats = oddspapi.parse_odds(
            body, fixture_id, index, fx, fx.get("home_team"), fx.get("away_team"), fx.get("start_time"),
            self.keep_books, self.reference_books, datetime.now(UTC).isoformat(),
        )
        df = self._enrich(rows)
        self._merge("oddspapi_latest", df, [fixture_id])
        return {"provider": "oddspapi", "raw_file": str(raw), "rows": df.height, **stats}

    def op_closing(self, fixture_id: str, books: list[str] | None = None) -> dict:
        """Closing line = last snapshot before kickoff, per book. One request, max 3 books."""
        fx = self._fixture(fixture_id)
        kickoff = oddspapi._parse_ts(fx.get("start_time"))
        if not kickoff or kickoff > datetime.now(UTC):
            raise ValueError("Closing lines are only available after kickoff")
        books = (books or ["pinnacle", "draftkings", "fanduel"])[:3]
        index = self.op_market_index()
        body = self._op_call(
            "/historical-odds", f"oddspapi:closing:{fixture_id}:{','.join(books)}", timedelta(days=3650),
            fixtureId=fixture_id, bookmakers=",".join(books),
        )
        raw = save_raw(self.dir, "oddspapi", f"historical_{fixture_id}", body)
        rows, stats = oddspapi.parse_odds(
            body, fixture_id, index, fx, fx.get("home_team"), fx.get("away_team"), fx.get("start_time"),
            set(books), set(), datetime.now(UTC).isoformat(), closing_before=kickoff,
        )
        df = self._enrich(rows)
        self._merge("oddspapi_closing", df, [fixture_id])
        return {"provider": "oddspapi", "raw_file": str(raw), "rows": df.height, **stats}

    # ---- enrichment: game ids, player ids, main lines --------------------------------------------
    def _game_for(self, a: str | None, b: str | None, when: str | None) -> dict | None:
        if not a or not b:
            return None
        sched = self.nfl.load("schedules")
        games = sched.filter(
            ((pl.col("home_team") == a) & (pl.col("away_team") == b))
            | ((pl.col("home_team") == b) & (pl.col("away_team") == a))
        )
        if games.height == 0:
            return None
        if when and games.height > 1:
            target = str(when)[:10]
            games = games.with_columns(
                (pl.col("gameday").cast(pl.Utf8).str.to_date() - pl.lit(target).str.to_date())
                .dt.total_days().abs().alias("_d")
            ).sort("_d")
        g = games.row(0, named=True)
        return {"game_id": g["game_id"], "season": g["season"], "week": g["week"],
                "home_team": g["home_team"], "away_team": g["away_team"]}

    def _enrich(self, rows: list[OddsRow]) -> pl.DataFrame:
        if not rows:
            return pl.DataFrame(schema=ROW_SCHEMA)
        records = [r.to_dict() for r in rows]
        game_cache: dict[tuple, dict | None] = {}
        report: dict[tuple, dict] = {}
        now = datetime.now(UTC).isoformat()
        for rec in records:
            gkey = (rec["home_team"], rec["away_team"], (rec["commence_time"] or "")[:10])
            if gkey not in game_cache:
                game_cache[gkey] = self._game_for(*gkey)
            game = game_cache[gkey] or {}
            rec["game_id"], rec["season"], rec["week"] = game.get("game_id"), game.get("season"), game.get("week")
            if game:  # trust nflverse for home/away orientation
                if rec["home_team"] != game["home_team"] and rec["side"] in ("home", "away"):
                    rec["side"] = "away" if rec["side"] == "home" else "home"
                rec["home_team"], rec["away_team"] = game["home_team"], game["away_team"]
            rec["gsis_id"] = rec["match_method"] = rec["match_score"] = None
            if rec["market"] in PROP_MARKETS and rec["player_name"]:
                teams = {t for t in (rec["home_team"], rec["away_team"]) if t}
                result = self.matcher.match(rec["player_name"], teams, rec["market"], rec["player_team"])
                rec["gsis_id"], rec["match_method"], rec["match_score"] = result.gsis_id, result.method, result.score
                rkey = (rec["provider"], rec["player_name"], tuple(sorted(teams)))
                report[rkey] = {
                    "provider": rec["provider"], "odds_name": rec["player_name"], "teams": "/".join(sorted(teams)),
                    "team_hint": rec["player_team"] or "", "market": rec["market"], "method": result.method,
                    "score": round(result.score, 1), "gsis_id": result.gsis_id or "",
                    "matched_name": result.matched_name or "", "seen_at": now,
                }
        self._write_match_report(report)
        df = pl.DataFrame(records, schema={k: v for k, v in ROW_SCHEMA.items() if k != "is_main"})
        return mark_main_lines(df)

    def _write_match_report(self, new: dict[tuple, dict]) -> None:
        path = self.dir / "match_report.csv"
        existing: dict[tuple, dict] = {}
        if path.exists():
            with path.open(newline="") as fh:
                for r in csv.DictReader(fh):
                    existing[(r["provider"], r["odds_name"], r["teams"])] = r
        for (prov, name, teams), row in new.items():
            existing[(prov, name, "/".join(teams))] = row
        with path.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=_REPORT_FIELDS)
            w.writeheader()
            w.writerows(sorted(existing.values(), key=lambda r: (r["method"] != "unmatched", r["odds_name"])))

    def _merge(self, name: str, df: pl.DataFrame, event_ids: list[str]) -> None:
        path = self.dir / "normalized" / f"{name}.parquet"
        if path.exists():
            old = pl.read_parquet(path).filter(~pl.col("event_id").is_in([e for e in event_ids if e]))
            df = pl.concat([old, df], how="diagonal_relaxed")
        tmp = path.with_suffix(".tmp")
        df.write_parquet(tmp)
        tmp.replace(path)

    # ---- reads (never call a paid API) ------------------------------------------------------
    def table(self, name: str) -> pl.DataFrame:
        path = self.dir / "normalized" / f"{name}.parquet"
        return pl.read_parquet(path) if path.exists() else pl.DataFrame(schema=ROW_SCHEMA)

    def odds(self, kind: str, week: int | None = None, event_id: str | None = None, market: str | None = None,
             book: str | None = None, main_only: bool = True, provider: str = "sgo") -> pl.DataFrame:
        df = self.table(f"{provider}_latest")
        markets = PROP_MARKETS if kind == "props" else ("h2h", "spread", "total")
        df = df.filter(pl.col("market").is_in(list(markets)))
        if week is not None:
            df = df.filter(pl.col("week") == week)
        if event_id:
            df = df.filter((pl.col("event_id") == event_id) | (pl.col("game_id") == event_id))
        if market:
            df = df.filter(pl.col("market") == market)
        if book:
            df = df.filter(pl.col("book") == book)
        if main_only and "is_main" in df.columns:
            df = df.filter(pl.col("is_main"))
        return df.sort(["commence_time", "game_id", "market", "player_name", "book", "side"], nulls_last=True)

    def events(self, week: int) -> list[dict]:
        games = self.nfl.week_games(week)
        sgo_df = self.table("sgo_latest")
        by_game: dict[str, dict] = {}
        if sgo_df.height:
            agg = sgo_df.filter(pl.col("game_id").is_not_null()).group_by("game_id").agg(
                pl.col("event_id").first().alias("sgo_event_id"),
                pl.col("fetched_at").max().alias("odds_fetched_at"),
                pl.col("market").is_in(list(PROP_MARKETS)).sum().alias("prop_quotes"),
                (~pl.col("market").is_in(list(PROP_MARKETS))).sum().alias("line_quotes"),
            )
            by_game = {r["game_id"]: r for r in agg.iter_rows(named=True)}
        fixtures = {fx.get("game_id"): fid for fid, fx in self._op_meta().get("fixtures", {}).items()}
        out = []
        for g in games.iter_rows(named=True):
            extra = by_game.get(g["game_id"], {})
            out.append({**g, "gameday": str(g["gameday"]),
                        "sgo_event_id": extra.get("sgo_event_id"),
                        "oddspapi_fixture_id": fixtures.get(g["game_id"]),
                        "odds_fetched_at": extra.get("odds_fetched_at"),
                        "prop_quotes": extra.get("prop_quotes", 0),
                        "line_quotes": extra.get("line_quotes", 0)})
        return out

    def usage_summary(self) -> dict:
        return {
            "sgo": {**self.sgo_guard.estimate(0, "objects"), "last_call": self.usage.last("sgo")},
            "oddspapi": {**self.op_guard.estimate(0, "requests"), "last_call": self.usage.last("oddspapi")},
            "min_refresh_minutes": self.settings.odds_min_refresh_minutes,
        }

    def match_report(self) -> list[dict]:
        path = self.dir / "match_report.csv"
        if not path.exists():
            return []
        with path.open(newline="") as fh:
            return list(csv.DictReader(fh))


def mark_main_lines(df: pl.DataFrame) -> pl.DataFrame:
    """When a book offers several lines for one player+market, use the provider's own main-line
    flag if it sent one; otherwise the main line is the one whose Over/Under prices are closest
    to even. Game lines and single-line props are always main."""
    if df.height == 0:
        return df.with_columns(pl.lit(None, dtype=pl.Boolean).alias("is_main"))
    keys = ["provider", "event_id", "market", "book", "provider_player_id"]
    two_sided = (
        df.filter(pl.col("side").is_in(["over", "under"]) & pl.col("line").is_not_null())
        .group_by([*keys, "line"])
        .agg((1 / pl.col("price_decimal")).alias("p"), pl.len().alias("n"))
        .filter(pl.col("n") == 2)
        .with_columns((pl.col("p").list.get(0) - pl.col("p").list.get(1)).abs().alias("gap"))
        .sort("gap")
        .group_by(keys, maintain_order=True)
        .first()
        .select([*keys, pl.col("line").alias("_main_line")])
    )
    df = df.join(two_sided, on=keys, how="left", nulls_equal=True)
    heuristic = pl.col("_main_line").is_null() | (pl.col("line") == pl.col("_main_line"))
    if "provider_main" in df.columns:
        heuristic = pl.when(pl.col("provider_main").is_not_null()).then(pl.col("provider_main")).otherwise(heuristic)
    return df.with_columns(heuristic.alias("is_main")).drop("_main_line")
