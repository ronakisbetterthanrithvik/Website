"""Settings loaded from stackd/.env (or the process environment)."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
STACKD_DIR = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(STACKD_DIR / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    sgo_api_key: str = Field(default="", alias="SGO_API_KEY")
    oddspapi_api_key: str = Field(default="", alias="ODDSPAPI_API_KEY")

    data_dir: Path = Field(default=BACKEND_DIR / "data", alias="STACKD_DATA_DIR")
    overrides_csv: Path = Field(
        default=BACKEND_DIR / "overrides" / "player_name_overrides.csv",
        alias="STACKD_OVERRIDES_CSV",
    )

    # Canonical book keys we keep lines for. Provider adapters map their own ids onto these.
    books: list[str] = Field(
        default=["draftkings", "fanduel", "betmgm", "caesars"], alias="STACKD_BOOKS"
    )
    # Sharp reference book(s); stored but never recommended as "place at".
    reference_books: list[str] = Field(default=["pinnacle"], alias="STACKD_REFERENCE_BOOKS")

    nflverse_max_age_hours: float = Field(default=24, alias="STACKD_NFLVERSE_MAX_AGE_HOURS")
    odds_min_refresh_minutes: float = Field(default=30, alias="STACKD_ODDS_MIN_REFRESH_MINUTES")

    # SportsGameOdds bills per object (1 event = 1 object).
    sgo_monthly_objects: int = Field(default=1000, alias="STACKD_SGO_MONTHLY_OBJECTS")
    sgo_object_floor: int = Field(default=100, alias="STACKD_SGO_OBJECT_FLOOR")

    # OddsPapi bills per request.
    oddspapi_monthly_requests: int = Field(default=250, alias="STACKD_ODDSPAPI_MONTHLY_REQUESTS")
    oddspapi_request_floor: int = Field(default=25, alias="STACKD_ODDSPAPI_REQUEST_FLOOR")
    oddspapi_nfl_tournament_id: str = Field(default="", alias="STACKD_ODDSPAPI_NFL_TOURNAMENT_ID")

    fuzzy_match_threshold: float = Field(default=90, alias="STACKD_FUZZY_MATCH_THRESHOLD")

    @property
    def nflverse_dir(self) -> Path:
        return self.data_dir / "nflverse"

    @property
    def odds_dir(self) -> Path:
        return self.data_dir / "odds"


@lru_cache
def get_settings() -> Settings:
    return Settings()
