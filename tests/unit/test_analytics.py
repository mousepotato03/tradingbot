from decimal import Decimal as D

import pytest

from app.analytics import (
    atr,
    bollinger,
    ema,
    macd,
    position_size,
    reward_risk,
    rsi,
    safe_ratio,
    sma,
)
from app.evaluation import excursions, forward_outcomes, reference_date


def test_moving_averages_known_values():
    assert sma([1, 2, 3, 4, 5], 3) == 4
    assert ema([1, 2, 3, 4, 5], 3) == 4
    assert macd([100] * 50) == {"macd": 0, "signal": 0, "histogram": 0}


@pytest.mark.parametrize(
    "values,expected", [([100] * 20, 50), (list(range(1, 25)), 100), (list(range(25, 1, -1)), 0)]
)
def test_rsi_boundary_cases(values, expected):
    assert rsi(values) == expected


def test_volatility_and_bands():
    assert atr([102] * 20, [98] * 20, [100] * 20) == 4
    assert bollinger([100] * 20) == {"middle": 100, "upper": 100, "lower": 100}


@pytest.mark.parametrize(
    "entry,stop,target", [(100, 100, 120), (100, 110, 120), (100, 90, 90), (0, -1, 10)]
)
def test_invalid_plan_geometry(entry, stop, target):
    with pytest.raises(ValueError):
        reward_risk(D(entry), D(stop), D(target))


def test_rr_and_sizing_include_costs_and_existing_exposure():
    assert reward_risk(D(100), D(90), D(120)) == 2
    assert position_size(D(10000), D(".01"), D(100), D(90), D(10000), D(".20")) == 10
    assert position_size(D(10000), D(".01"), D(100), D(90), D(10000), D(".20"), fee=D(".01")) == 8
    assert (
        position_size(D(10000), D(".01"), D(100), D(90), D(10000), D(".20"), existing_value=D(2000))
        == 0
    )


def test_analytics_reject_bad_inputs():
    with pytest.raises(ValueError):
        sma([1, float("nan")], 2)
    with pytest.raises(ValueError):
        rsi([100])
    with pytest.raises(ValueError):
        safe_ratio(D(1), D(0))


def test_forward_outcomes_and_actual_high_low_excursions():
    outcomes = forward_outcomes([D(100), D(110), D(90)], [D(100), D(105), D(100)])
    assert outcomes["1"]["return"] == "0.1"
    assert outcomes["1"]["benchmark_relative"] == "0.05"
    assert "5" not in outcomes and outcomes["realized_r"] is None
    assert excursions([D(110), D(120)], [D(95), D(90)], D(100)) == {"mfe": "0.2", "mae": "-0.1"}


def test_excursions_are_zero_when_no_favorable_or_adverse_move():
    assert excursions([D(90)], [D(80)], D(100))["mfe"] == "0"
    assert excursions([D(120)], [D(110)], D(100))["mae"] == "0"
    with pytest.raises(ValueError):
        excursions([D("NaN")], [D(90)], D(100))


def test_reference_close_never_precedes_an_after_close_report():
    from datetime import date, datetime

    assert reference_date(datetime.fromisoformat("2026-09-28T19:59:00+00:00")) == date(2026, 9, 28)
    assert reference_date(datetime.fromisoformat("2026-09-28T20:00:00+00:00")) == date(2026, 9, 29)
