"""Map sportsbook player names to nflverse gsis_ids.

Order of precedence: manual overrides CSV → exact normalized match → fuzzy match.
Candidates are always restricted to the two teams in the game (and, when known, to
positions the market applies to), so a fuzzy match can't jump to a different team.
"""

import csv
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz, process

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

# Markets → positions whose players can appear in them.
MARKET_POSITIONS: dict[str, set[str]] = {
    "pass_yds": {"QB"},
    "rush_yds": {"QB", "RB", "WR", "TE", "FB"},
    "receptions": {"RB", "WR", "TE", "FB"},
    "rec_yds": {"RB", "WR", "TE", "FB"},
    "anytime_td": {"QB", "RB", "WR", "TE", "FB"},
}


def normalize_name(name: str) -> str:
    """'D.J. Moore' → 'dj moore'; 'Marvin Harrison Jr.' → 'marvin harrison'; 'Cunningham, Cade' → 'cade cunningham'."""
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    text = text.strip()
    if text.count(",") == 1:
        last, first = (part.strip() for part in text.split(","))
        # "Harrison Jr., Marvin" and "Marvin Harrison, Jr." both occur; only flip when the
        # part after the comma isn't just a suffix.
        if first.lower().rstrip(".") not in _SUFFIXES:
            text = f"{first} {last}"
        else:
            text = last
    text = text.lower()
    text = re.sub(r"[.'’`]", "", text)
    text = re.sub(r"[-_]", " ", text)
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    tokens = [t for t in text.split() if t not in _SUFFIXES]
    return " ".join(tokens)


@dataclass(frozen=True)
class Candidate:
    gsis_id: str
    full_name: str
    team: str
    position: str


@dataclass(frozen=True)
class MatchResult:
    gsis_id: str | None
    method: str  # "override" | "exact" | "fuzzy" | "unmatched" | "ambiguous"
    score: float
    matched_name: str | None = None


def load_overrides(path: Path) -> dict[tuple[str, str], str]:
    """CSV columns: odds_name,team,gsis_id. team may be blank to apply to any team."""
    overrides: dict[tuple[str, str], str] = {}
    if not path.exists():
        return overrides
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            name = normalize_name(row.get("odds_name", ""))
            gsis = (row.get("gsis_id") or "").strip()
            if name and gsis:
                overrides[(name, (row.get("team") or "").strip().upper())] = gsis
    return overrides


class NameMatcher:
    def __init__(
        self,
        candidates: list[Candidate],
        overrides: dict[tuple[str, str], str] | None = None,
        threshold: float = 90,
    ):
        self.candidates = candidates
        self.overrides = overrides or {}
        self.threshold = threshold
        self._by_gsis = {c.gsis_id: c for c in candidates}

    def match(
        self,
        name: str,
        teams: set[str] | None = None,
        market: str | None = None,
        team_hint: str | None = None,
    ) -> MatchResult:
        norm = normalize_name(name)
        if not norm:
            return MatchResult(None, "unmatched", 0)

        for team_key in ((team_hint or "").upper(), *sorted(teams or []), ""):
            gsis = self.overrides.get((norm, team_key))
            if gsis:
                cand = self._by_gsis.get(gsis)
                return MatchResult(gsis, "override", 100, cand.full_name if cand else None)

        pool = self._pool(teams, market)
        if team_hint:
            hinted = [c for c in pool if c.team == team_hint]
            pool = hinted or pool

        exact = [c for c in pool if normalize_name(c.full_name) == norm]
        if len(exact) == 1:
            return MatchResult(exact[0].gsis_id, "exact", 100, exact[0].full_name)
        if len(exact) > 1:
            return MatchResult(None, "ambiguous", 100)

        if not pool:
            return MatchResult(None, "unmatched", 0)
        choices = {i: normalize_name(c.full_name) for i, c in enumerate(pool)}
        scored = process.extract(norm, choices, scorer=fuzz.token_sort_ratio, limit=2)
        if not scored or scored[0][1] < self.threshold:
            best = scored[0][1] if scored else 0
            return MatchResult(None, "unmatched", best)
        if len(scored) > 1 and scored[1][1] >= self.threshold and scored[0][1] - scored[1][1] < 3:
            return MatchResult(None, "ambiguous", scored[0][1])
        cand = pool[scored[0][2]]
        return MatchResult(cand.gsis_id, "fuzzy", scored[0][1], cand.full_name)

    def _pool(self, teams: set[str] | None, market: str | None) -> list[Candidate]:
        pool = self.candidates
        if teams:
            pool = [c for c in pool if c.team in teams]
        positions = MARKET_POSITIONS.get(market or "")
        if positions:
            filtered = [c for c in pool if c.position in positions]
            pool = filtered or pool
        return pool
