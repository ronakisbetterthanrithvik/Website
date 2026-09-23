import httpx
import pytest

from stackd.odds import sgo
from stackd.odds.base import BudgetError, ConfirmationRequired, TooSoonError
from stackd.odds.service import OddsService
from tests.conftest import load_fixture


def test_parse_events_maps_markets_books_and_sides():
    events = load_fixture("sgo_events.json")["data"]
    rows, stats = sgo.parse_events(events, {"draftkings", "fanduel", "betmgm", "caesars"}, {"pinnacle"}, "t")
    by = {(r.market, r.book, r.side, r.player_name): r for r in rows}

    dk_over = by[("pass_yds", "draftkings", "over", "Patrick Mahomes")]
    assert (dk_over.line, dk_over.price_american, dk_over.home_team, dk_over.away_team) == (262.5, -115, "BUF", "KC")
    assert by[("pass_yds", "pinnacle", "over", "Patrick Mahomes")].is_reference
    assert ("pass_yds", "betmgm", "over", "Patrick Mahomes") not in by  # available: false
    assert "bovada" in stats["unknown_books"]

    assert by[("rush_yds", "caesars", "over", "James Cook")].price_american == 100
    td = by[("anytime_td", "fanduel", "yes", "James Cook")]
    assert td.line is None and td.price_american == 105

    # 1st-half market ignored, full-game kept
    recs = [r for r in rows if r.market == "receptions"]
    assert len(recs) == 1 and recs[0].line == 4.5 and recs[0].player_name == "D.J. Moore"

    assert by[("spread", "draftkings", "home", None)].line == -2.5
    assert by[("total", "draftkings", "over", None)].line == 48.5
    assert by[("h2h", "betmgm", "away", None)].price_american == 120


def _mock_transport(counter):
    body = load_fixture("sgo_events.json")

    def handler(request: httpx.Request):
        counter.append(request)
        assert request.headers["x-api-key"] == "test-sgo"
        assert request.url.params["leagueID"] == "NFL"
        return httpx.Response(200, json=body, headers={"x-ratelimit-remaining": "9"})

    return httpx.MockTransport(handler)


def test_week_refresh_needs_confirmation_then_spends_once(settings, fake_nfl):
    calls = []
    svc = OddsService(settings, fake_nfl, sgo_client=sgo.SGOClient("test-sgo", transport=_mock_transport(calls)))

    with pytest.raises(ConfirmationRequired) as exc:
        svc.refresh_sgo_week(4)
    assert exc.value.estimate["estimated_cost"] == 2
    assert calls == []  # nothing spent without confirm

    out = svc.refresh_sgo_week(4, confirm=True)
    assert len(calls) == 1 and out["objects_used"] == 2
    assert svc.usage.month_to_date("sgo") == 2
    assert "x-ratelimit-remaining" in svc.usage.last("sgo")["headers"]

    props = svc.odds("props", week=4)
    mahomes = props.filter(props["player_name"] == "Patrick Mahomes")
    assert set(mahomes["gsis_id"]) == {"00-0033873"}
    assert set(mahomes["game_id"]) == {"2026_04_KC_BUF"}
    moore = props.filter(props["player_name"] == "D.J. Moore")
    assert moore["match_method"][0] == "exact"

    # 30-minute floor: the week and every event in it are now cached
    with pytest.raises(TooSoonError):
        svc.refresh_sgo_week(4, confirm=True)
    with pytest.raises(TooSoonError):
        svc.refresh_sgo_event("evt_KC_BUF")
    assert len(calls) == 1

    events = {e["game_id"]: e for e in svc.events(4)}
    assert events["2026_04_KC_BUF"]["sgo_event_id"] == "evt_KC_BUF"
    assert events["2026_04_KC_BUF"]["prop_quotes"] > 0


def test_budget_floor_blocks_spend(settings, fake_nfl):
    settings.sgo_monthly_objects = 11  # floor is 10, week costs 2
    calls = []
    svc = OddsService(settings, fake_nfl, sgo_client=sgo.SGOClient("test-sgo", transport=_mock_transport(calls)))
    with pytest.raises(BudgetError):
        svc.refresh_sgo_week(4, confirm=True)
    assert calls == []
