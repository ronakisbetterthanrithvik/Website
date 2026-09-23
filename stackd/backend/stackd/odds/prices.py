"""Odds format conversions shared by every provider adapter (and later, parlay math)."""


def american_to_decimal(american: float) -> float:
    if american == 0:
        raise ValueError("American odds cannot be 0")
    if american > 0:
        return 1 + american / 100
    return 1 + 100 / -american


def decimal_to_american(decimal: float) -> int:
    if decimal <= 1:
        raise ValueError("Decimal odds must be greater than 1")
    if decimal >= 2:
        return round((decimal - 1) * 100)
    return round(-100 / (decimal - 1))


def parse_american(value: str | int | float | None) -> int | None:
    """Parse '+120', '-110', 'EVEN', 120 → int. Returns None when missing or unparseable."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value) if value != 0 else None
    text = str(value).strip().upper()
    if text in ("EVEN", "EV"):
        return 100
    try:
        parsed = int(float(text.replace("+", "")))
    except ValueError:
        return None
    return parsed or None


def implied_probability(decimal: float) -> float:
    return 1 / decimal
