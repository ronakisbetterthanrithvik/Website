"""Provider-agnostic pieces: the normalized odds row, usage logging, and spend guards.

Every paid call goes through `PaidCallGuard.check()` first. The guard enforces:
  * a minimum refresh interval per cache key (30 min by default; `force` cannot bypass it)
  * a monthly budget floor computed from our own usage log
"""

import csv
import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

CANONICAL_MARKETS = ("pass_yds", "rush_yds", "receptions", "rec_yds", "anytime_td", "h2h", "spread", "total")
PROP_MARKETS = ("pass_yds", "rush_yds", "receptions", "rec_yds", "anytime_td")


@dataclass
class OddsRow:
    provider: str
    event_id: str
    commence_time: str | None
    home_team: str | None
    away_team: str | None
    market: str
    book: str
    side: str  # over | under | yes | no | home | away
    price_american: int
    price_decimal: float
    line: float | None = None
    player_name: str | None = None
    provider_player_id: str | None = None
    player_team: str | None = None
    last_update: str | None = None
    fetched_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    is_reference: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class TooSoonError(Exception):
    def __init__(self, key: str, last: datetime, next_allowed: datetime):
        self.key, self.last, self.next_allowed = key, last, next_allowed
        super().__init__(
            f"{key} was fetched at {last:%H:%M:%S} UTC; next fetch allowed at {next_allowed:%H:%M:%S} UTC. Serving cache."
        )


class BudgetError(Exception):
    pass


class ConfirmationRequired(Exception):
    def __init__(self, estimate: dict):
        self.estimate = estimate
        super().__init__(f"Paid call needs confirmation: {estimate}")


_USAGE_FIELDS = ["timestamp", "provider", "endpoint", "params", "cost", "unit", "status", "headers"]
_lock = threading.Lock()


class UsageLog:
    """Append-only CSV of every paid request, with any rate-limit headers the provider sent."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, provider: str, endpoint: str, params: dict, cost: int, unit: str, status: int, headers: dict) -> None:
        safe_params = {k: v for k, v in params.items() if "key" not in k.lower()}
        interesting = {
            k: v for k, v in headers.items()
            if any(w in k.lower() for w in ("limit", "remaining", "usage", "used", "credit", "quota"))
        }
        row = {
            "timestamp": datetime.now(UTC).isoformat(),
            "provider": provider,
            "endpoint": endpoint,
            "params": json.dumps(safe_params, sort_keys=True),
            "cost": cost,
            "unit": unit,
            "status": status,
            "headers": json.dumps(interesting, sort_keys=True),
        }
        with _lock:
            new = not self.path.exists()
            with self.path.open("a", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=_USAGE_FIELDS)
                if new:
                    writer.writeheader()
                writer.writerow(row)

    def rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open(newline="") as fh:
            return list(csv.DictReader(fh))

    def month_to_date(self, provider: str, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        month = now.strftime("%Y-%m")
        return sum(
            int(r["cost"] or 0)
            for r in self.rows()
            if r["provider"] == provider and r["timestamp"].startswith(month)
        )

    def last(self, provider: str) -> dict | None:
        rows = [r for r in self.rows() if r["provider"] == provider]
        return rows[-1] if rows else None


class FetchLog:
    """Remembers when each cache key was last fetched from a paid API."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict[str, str]:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def last(self, key: str) -> datetime | None:
        value = self._read().get(key)
        return datetime.fromisoformat(value) if value else None

    def mark(self, *keys: str, when: datetime | None = None) -> None:
        when = when or datetime.now(UTC)
        with _lock:
            data = self._read()
            for key in keys:
                data[key] = when.isoformat()
            self.path.write_text(json.dumps(data, indent=2, sort_keys=True))


class PaidCallGuard:
    def __init__(
        self,
        provider: str,
        usage: UsageLog,
        fetches: FetchLog,
        monthly_allowance: int,
        floor: int,
        min_interval: timedelta,
    ):
        self.provider = provider
        self.usage = usage
        self.fetches = fetches
        self.monthly_allowance = monthly_allowance
        self.floor = floor
        self.min_interval = min_interval

    def remaining_estimate(self) -> int:
        return self.monthly_allowance - self.usage.month_to_date(self.provider)

    def check_interval(self, keys: list[str], min_interval: timedelta | None = None) -> None:
        interval = self.min_interval if min_interval is None else min_interval
        now = datetime.now(UTC)
        for key in keys:
            last = self.fetches.last(key)
            if last and now - last < interval:
                raise TooSoonError(key, last, last + interval)

    def check_budget(self, estimated_cost: int) -> None:
        remaining = self.remaining_estimate()
        if remaining - estimated_cost < self.floor:
            raise BudgetError(
                f"{self.provider}: ~{remaining} left this month by our log; this call needs ~{estimated_cost} "
                f"and the floor is {self.floor}. Raise the floor setting or wait for the monthly reset."
            )

    def estimate(self, estimated_cost: int, unit: str, detail: dict | None = None) -> dict:
        return {
            "provider": self.provider,
            "estimated_cost": estimated_cost,
            "unit": unit,
            "used_this_month": self.usage.month_to_date(self.provider),
            "monthly_allowance": self.monthly_allowance,
            "remaining_estimate": self.remaining_estimate(),
            "floor": self.floor,
            **(detail or {}),
        }


def save_raw(dir_: Path, provider: str, label: str, payload) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = dir_ / "raw" / provider / f"{stamp}_{label}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return path
