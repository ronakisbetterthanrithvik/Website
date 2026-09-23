from datetime import datetime, timezone

import httpx
import pytest

from stackd.odds import oddspapi
from stackd.odds.service import OddsService, mark_main_lines
from tests.conftest import load_fixture

FIXTURE = {"fixture_id": "fx1", "p1": "BUF", "p2": "KC", "start_time": "2026-09-27T20:25:00Z"}


def test_market_index_skips_periods():
    index = oddspapi.build_market_index(load_fixture("oddspapi_markets.json"))
    assert index["1010"]["market"] == "pass_yds" and index["1010"]["handicap"] == 260.5
    assert "1020" not in index  # 1st half
    assert index["131"]["market"] == "h2h"


def test_parse_live_odds():
    index = oddspapi.build_market_index(load_fixture("oddspapi_markets.json"))
    rows, stats = oddspapi.parse_odds(load_fixture("oddspapi_odds.json"), "fx1", index, FIXTURE, "BUF", "KC",
                                      FIXTURE["start_time"], {"draftkings"}, {"pinnacle"}, "t")
    assert all(r.book == "pinnacle" for r in rows)  # bet365 not kept
    props = [r for r in rows if r.market == "pass_yds"]
    assert {r.player_name for r in props} == {"Mahomes, Patrick"}
    assert {(r.line, r.side) for r in props} == {(260.5, "over"), (260.5, "under"), (275.5, "over"), (275.5, "under")}
    ml = {r.side: r.price_american for r in rows if r.market == "h2h"}
    assert ml == {"home": -125, "away": 110}
    spread = [r for r in rows if r.market == "spread"]
    assert len(spread) == 1 and spread[0].side == "home" and spread[0].line == -2.5  # inactive side dropped


def test_main_line_is_closest_to_even():
    import polars as pl
    from stackd.odds.service import ROW_SCHEMA

    index = oddspapi.build_market_index(load_fixture("oddspapi_markets.json"))
    rows, _ = oddspapi.parse_odds(load_fixture("oddspapi_odds.json"), "fx1", index, FIXTURE, "BUF", "KC",
                                  None, set(), {"pinnacle"}, "t")
    df = pl.DataFrame([r.to_dict() for r in rows])
    main = mark_main_lines(df).filter(pl.col("is_main") & (pl.col("market") == "pass_yds"))
    assert set(main["line"]) == {260.5}


def test_closing_uses_last_snapshot_before_kickoff():
    index = oddspapi.build_market_index(load_fixture("oddspapi_markets.json"))
    kickoff = datetime(2026, 9, 27, 20, 25, tzinfo=timezone.utc)
    rows, _ = oddspapi.parse_odds(load_fixture("oddspapi_historical.json"), "fx1", index, FIXTURE, "BUF", "KC",
                                  None, {"pinnacle"}, set(), "t", closing_before=kickoff)
    assert len(rows) == 1
    assert rows[0].price_decimal == 1.80 and rows[0].player_name == "Mahomes, Patrick"


def test_service_logs_each_request_and_matches_last_first_names(settings, fake_nfl):
    seen = []

    def handler(request):
        seen.append(request.url.path)
        assert request.url.params["apiKey"] == "test-op"
        if request.url.path.endswith("/markets"):
            return httpx.Response(200, json=load_fixture("oddspapi_markets.json"))
        return httpx.Response(200, json=load_fixture("oddspapi_odds.json"))

    client = oddspapi.OddsPapiClient("test-op", transport=httpx.MockTransport(handler))
    client.MIN_GAP_SECONDS = 0
    svc = OddsService(settings, fake_nfl, oddspapi_client=client)
    svc._op_save_meta({"fixtures": {"fx1": {**FIXTURE, "game_id": "2026_04_KC_BUF", "home_team": "BUF", "away_team": "KC"}}})

    out = svc.op_odds("fx1")
    assert out["rows"] > 0
    assert svc.usage.month_to_date("oddspapi") == 2  # markets catalog + odds
    assert "apiKey" not in svc.usage.last("oddspapi")["params"]
    df = svc.table("oddspapi_latest")
    assert set(df.filter(df["market"] == "pass_yds")["gsis_id"]) == {"00-0033873"}


def test_catalog_filters_other_sports_and_periods():
    index = oddspapi.build_market_index(load_fixture("oddspapi_markets.json"))
    assert "999" not in index and "998" not in index


def test_provider_main_line_flag_wins():
    import polars as pl

    rows = [
        {"provider": "oddspapi", "event_id": "e", "market": "pass_yds", "book": "pinnacle", "provider_player_id": "1",
         "side": s, "line": line, "price_decimal": price, "provider_main": main}
        for s, line, price, main in [("over", 250.5, 1.5, False), ("under", 250.5, 2.6, False),
                                     ("over", 260.5, 2.0, True), ("under", 260.5, 1.8, True)]
    ]
    out = mark_main_lines(pl.DataFrame(rows))
    assert set(out.filter(pl.col("is_main"))["line"]) == {260.5}
