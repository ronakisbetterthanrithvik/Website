"""Team identity: nflverse abbreviations, full names, colors, and provider-name lookup."""

import re

# nflverse abbreviation -> (full name, primary color). Colors are a static fallback so the
# UI can draw initials circles even before nflverse `load_teams` has been cached.
TEAMS: dict[str, tuple[str, str]] = {
    "ARI": ("Arizona Cardinals", "#97233F"),
    "ATL": ("Atlanta Falcons", "#A71930"),
    "BAL": ("Baltimore Ravens", "#241773"),
    "BUF": ("Buffalo Bills", "#00338D"),
    "CAR": ("Carolina Panthers", "#0085CA"),
    "CHI": ("Chicago Bears", "#0B162A"),
    "CIN": ("Cincinnati Bengals", "#FB4F14"),
    "CLE": ("Cleveland Browns", "#FF3C00"),
    "DAL": ("Dallas Cowboys", "#003594"),
    "DEN": ("Denver Broncos", "#FB4F14"),
    "DET": ("Detroit Lions", "#0076B6"),
    "GB": ("Green Bay Packers", "#203731"),
    "HOU": ("Houston Texans", "#A71930"),
    "IND": ("Indianapolis Colts", "#002C5F"),
    "JAX": ("Jacksonville Jaguars", "#006778"),
    "KC": ("Kansas City Chiefs", "#E31837"),
    "LV": ("Las Vegas Raiders", "#A5ACAF"),
    "LAC": ("Los Angeles Chargers", "#0080C6"),
    "LA": ("Los Angeles Rams", "#003594"),
    "MIA": ("Miami Dolphins", "#008E97"),
    "MIN": ("Minnesota Vikings", "#4F2683"),
    "NE": ("New England Patriots", "#002244"),
    "NO": ("New Orleans Saints", "#D3BC8D"),
    "NYG": ("New York Giants", "#0B2265"),
    "NYJ": ("New York Jets", "#125740"),
    "PHI": ("Philadelphia Eagles", "#004C54"),
    "PIT": ("Pittsburgh Steelers", "#FFB612"),
    "SEA": ("Seattle Seahawks", "#69BE28"),
    "SF": ("San Francisco 49ers", "#AA0000"),
    "TB": ("Tampa Bay Buccaneers", "#D50A0A"),
    "TEN": ("Tennessee Titans", "#4B92DB"),
    "WAS": ("Washington Commanders", "#5A1414"),
}

_EXTRA_ALIASES = {
    "LAR": "LA",
    "JAC": "JAX",
    "WSH": "WAS",
    "washington football team": "WAS",
    "oakland raiders": "LV",
    "san diego chargers": "LAC",
    "st louis rams": "LA",
}


def _key(text: str) -> str:
    text = re.sub(r"_NFL$", "", text.strip(), flags=re.IGNORECASE)
    text = text.replace("_", " ").replace(".", "")
    return re.sub(r"\s+", " ", text).strip().lower()


_LOOKUP: dict[str, str] = {}
for _abbr, (_name, _color) in TEAMS.items():
    _LOOKUP[_key(_abbr)] = _abbr
    _LOOKUP[_key(_name)] = _abbr
    _LOOKUP[_key(_name.rsplit(" ", 1)[-1])] = _abbr  # nickname: "Chiefs"
for _alias, _abbr in _EXTRA_ALIASES.items():
    _LOOKUP[_key(_alias)] = _abbr


def to_abbr(name: str | None) -> str | None:
    """Map any provider team name/id ("Kansas City Chiefs", "KANSAS_CITY_CHIEFS_NFL", "KC") to nflverse abbr."""
    if not name:
        return None
    return _LOOKUP.get(_key(name))


def team_color(abbr: str) -> str:
    return TEAMS.get(abbr, ("", "#3A3F4B"))[1]
