import json
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from stackd.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


class FakeNfl:
    """Stands in for NflverseCache with tiny in-memory tables (no network)."""

    season = 2026

    def __init__(self):
        self.tables = {
            "schedules": pl.DataFrame({
                "game_id": ["2026_04_KC_BUF", "2026_04_DAL_NYG", "2026_05_BUF_KC"],
                "season": [2026, 2026, 2026],
                "week": [4, 4, 5],
                "game_type": ["REG"] * 3,
                "gameday": [date(2026, 9, 27), date(2026, 9, 27), date(2026, 10, 4)],
                "gametime": ["16:25", "20:20", "13:00"],
                "home_team": ["BUF", "NYG", "KC"],
                "away_team": ["KC", "DAL", "BUF"],
                "spread_line": [2.5, -3.0, 1.0],
                "total_line": [48.5, 44.0, 47.0],
                "result": [None, None, None],
            }),
            "rosters": pl.DataFrame({
                "gsis_id": ["00-0033873", "00-0037248", "00-0034827", "00-0099999", "00-0088888"],
                "full_name": ["Patrick Mahomes", "James Cook", "DJ Moore", "Josh Allen", "Josh Allen"],
                "team": ["KC", "BUF", "BUF", "BUF", "JAX"],
                "position": ["QB", "RB", "WR", "QB", "LB"],
            }),
        }

    def load(self, name, refresh_if_stale=True):
        return self.tables[name]

    def week_games(self, week):
        from stackd.data.nflverse import NflverseCache
        return NflverseCache.week_games(self, week)

    def roster_candidates(self):
        from stackd.data.nflverse import NflverseCache
        return NflverseCache.roster_candidates(self)

    def current_week(self):
        return 4


@pytest.fixture
def settings(tmp_path):
    overrides = tmp_path / "overrides.csv"
    overrides.write_text("odds_name,team,gsis_id\n")
    return Settings(
        SGO_API_KEY="test-sgo", ODDSPAPI_API_KEY="test-op",
        STACKD_DATA_DIR=tmp_path / "data", STACKD_OVERRIDES_CSV=overrides,
        STACKD_SGO_MONTHLY_OBJECTS=1000, STACKD_SGO_OBJECT_FLOOR=10,
    )


@pytest.fixture
def fake_nfl():
    return FakeNfl()
