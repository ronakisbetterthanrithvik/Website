"""nflverse loaders with a parquet cache that refreshes once a day.

nflverse data is free, so refreshing on a schedule is fine. A failed download never
deletes the previous file; reads keep serving the last good copy.
"""

import json
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import nflreadpy as nfl
import polars as pl

from stackd.config import Settings, get_settings
from stackd.data.names import Candidate

log = logging.getLogger(__name__)

SeasonLoader = Callable[[int], pl.DataFrame]

# name -> (loader, is_season_scoped)
DATASETS: dict[str, tuple[Callable[..., pl.DataFrame], bool]] = {
    "schedules": (lambda s: nfl.load_schedules(s), True),
    "player_stats": (lambda s: nfl.load_player_stats(s, summary_level="week"), True),
    "snap_counts": (lambda s: nfl.load_snap_counts(s), True),
    "depth_charts": (lambda s: nfl.load_depth_charts(s), True),
    "injuries": (lambda s: nfl.load_injuries(s), True),
    "rosters": (lambda s: nfl.load_rosters(s), True),
    "pbp": (lambda s: nfl.load_pbp(s), True),
    "players": (lambda _s: nfl.load_players(), False),
    "teams": (lambda _s: nfl.load_teams(), False),
}

_lock = threading.Lock()


class NflverseCache:
    def __init__(self, settings: Settings | None = None, season: int | None = None):
        self.settings = settings or get_settings()
        self.season = season or nfl.get_current_season()
        self.dir = self.settings.nflverse_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.meta_path = self.dir / "meta.json"

    # ---- paths / metadata -------------------------------------------------
    def path(self, name: str) -> Path:
        _, seasonal = DATASETS[name]
        return self.dir / (f"{name}_{self.season}.parquet" if seasonal else f"{name}.parquet")

    def _meta(self) -> dict:
        if self.meta_path.exists():
            return json.loads(self.meta_path.read_text())
        return {}

    def _write_meta(self, meta: dict) -> None:
        self.meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))

    def _meta_key(self, name: str) -> str:
        return self.path(name).name

    def age(self, name: str) -> timedelta | None:
        entry = self._meta().get(self._meta_key(name))
        if not entry or not self.path(name).exists():
            return None
        return datetime.now(UTC) - datetime.fromisoformat(entry["fetched_at"])

    def is_stale(self, name: str) -> bool:
        age = self.age(name)
        return age is None or age > timedelta(hours=self.settings.nflverse_max_age_hours)

    # ---- refresh / load ---------------------------------------------------
    def refresh(self, name: str) -> dict:
        loader, _ = DATASETS[name]
        started = datetime.now(UTC)
        try:
            df = loader(self.season)
        except Exception as exc:  # network, 404 early in a season, parse errors
            log.warning("nflverse refresh failed for %s: %s", name, exc)
            with _lock:
                meta = self._meta()
                entry = meta.get(self._meta_key(name), {})
                entry["last_error"] = f"{started.isoformat()}: {exc}"
                meta[self._meta_key(name)] = entry
                self._write_meta(meta)
            return {"dataset": name, "ok": False, "error": str(exc)}

        tmp = self.path(name).with_suffix(".tmp")
        df.write_parquet(tmp)
        tmp.replace(self.path(name))
        with _lock:
            meta = self._meta()
            meta[self._meta_key(name)] = {
                "fetched_at": started.isoformat(),
                "rows": df.height,
                "columns": df.width,
                "last_error": None,
            }
            self._write_meta(meta)
        return {"dataset": name, "ok": True, "rows": df.height}

    def refresh_stale(self, force: bool = False) -> list[dict]:
        return [self.refresh(n) for n in DATASETS if force or self.is_stale(n)]

    def load(self, name: str, refresh_if_stale: bool = True) -> pl.DataFrame:
        if refresh_if_stale and self.is_stale(name):
            self.refresh(name)
        path = self.path(name)
        if not path.exists():
            raise FileNotFoundError(f"No cached nflverse data for {name} ({path.name}); refresh failed or never ran")
        return pl.read_parquet(path)

    def status(self) -> list[dict]:
        meta = self._meta()
        out = []
        for name in DATASETS:
            entry = meta.get(self._meta_key(name), {})
            age = self.age(name)
            out.append(
                {
                    "dataset": name,
                    "file": self.path(name).name,
                    "cached": self.path(name).exists(),
                    "fetched_at": entry.get("fetched_at"),
                    "age_hours": round(age.total_seconds() / 3600, 2) if age else None,
                    "rows": entry.get("rows"),
                    "stale": self.is_stale(name),
                    "last_error": entry.get("last_error"),
                }
            )
        return out

    # ---- helpers used by the odds layer ------------------------------------
    def current_week(self) -> int:
        """First regular/post-season week that still has an unplayed game."""
        sched = self.load("schedules")
        upcoming = sched.filter(pl.col("result").is_null()).sort("gameday")
        if upcoming.height:
            return int(upcoming["week"][0])
        return int(sched["week"].max())

    def week_games(self, week: int) -> pl.DataFrame:
        cols = [
            "game_id", "season", "week", "game_type", "gameday", "gametime",
            "home_team", "away_team", "spread_line", "total_line",
            "home_moneyline", "away_moneyline", "home_score", "away_score",
        ]
        sched = self.load("schedules").filter(pl.col("week") == week)
        return sched.select([c for c in cols if c in sched.columns]).sort(["gameday", "gametime"])

    def roster_candidates(self) -> list[Candidate]:
        rosters = self.load("rosters")
        name_col = "full_name" if "full_name" in rosters.columns else "player_name"
        rows = (
            rosters.filter(pl.col("gsis_id").is_not_null())
            .select(["gsis_id", name_col, "team", "position"])
            .unique(subset=["gsis_id", "team"])
        )
        return [
            Candidate(gsis_id=r[0], full_name=r[1] or "", team=r[2] or "", position=r[3] or "")
            for r in rows.iter_rows()
        ]

    def snap_counts_with_gsis(self) -> pl.DataFrame:
        """Snap counts are keyed by PFR id; attach gsis_id through the players crosswalk."""
        snaps = self.load("snap_counts")
        players = self.load("players")
        pfr_col = "pfr_id" if "pfr_id" in players.columns else "pfr_player_id"
        xwalk = players.select([pl.col(pfr_col).alias("pfr_player_id"), "gsis_id"]).drop_nulls()
        return snaps.join(xwalk, on="pfr_player_id", how="left")
