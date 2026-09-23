import pytest

from stackd.odds.prices import american_to_decimal, decimal_to_american, parse_american


def test_round_trip():
    assert american_to_decimal(-110) == pytest.approx(1.9091, abs=1e-4)
    assert american_to_decimal(150) == 2.5
    assert decimal_to_american(2.5) == 150
    assert decimal_to_american(1.9091) == -110
    assert decimal_to_american(2.0) == 100


def test_parse_american():
    assert parse_american("+120") == 120
    assert parse_american("-110") == -110
    assert parse_american("EVEN") == 100
    assert parse_american("") is None
    assert parse_american(None) is None
