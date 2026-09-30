from decimal import ROUND_FLOOR, Decimal

import numpy as np


def _series(values, period: int) -> np.ndarray:
    result = np.asarray(values, dtype=float)
    if period < 1 or result.ndim != 1 or len(result) < period or not np.isfinite(result).all():
        raise ValueError("Insufficient or nonfinite observations")
    return result


def sma(values, period: int) -> float:
    return float(_series(values, period)[-period:].mean())


def ema_series(values, period: int) -> list[float]:
    data = _series(values, period)
    current = float(data[:period].mean())
    result = [current]
    alpha = 2 / (period + 1)
    for value in data[period:]:
        current = float(alpha * value + (1 - alpha) * current)
        result.append(current)
    return result


def ema(values, period: int) -> float:
    return ema_series(values, period)[-1]


def rsi(values, period: int = 14) -> float:
    changes = np.diff(_series(values, period + 1))
    gains, losses = np.maximum(changes, 0), np.maximum(-changes, 0)
    gain, loss = float(gains[:period].mean()), float(losses[:period].mean())
    for up, down in zip(gains[period:], losses[period:], strict=True):
        gain = (gain * (period - 1) + up) / period
        loss = (loss * (period - 1) + down) / period
    if gain == loss == 0:
        return 50.0
    return 100.0 if loss == 0 else float(100 - 100 / (1 + gain / loss))


def macd(values, fast=12, slow=26, signal=9) -> dict[str, float]:
    _series(values, slow + signal - 1)
    fast_values, slow_values = ema_series(values, fast), ema_series(values, slow)
    line = np.asarray(fast_values[slow - fast :]) - np.asarray(slow_values)
    signal_value = ema(line, signal)
    return {
        "macd": float(line[-1]),
        "signal": signal_value,
        "histogram": float(line[-1] - signal_value),
    }


def bollinger(values, period=20, deviations=2) -> dict[str, float]:
    data = _series(values, period)[-period:]
    mid, deviation = float(data.mean()), float(data.std(ddof=0))
    return {
        "middle": mid,
        "upper": mid + deviations * deviation,
        "lower": mid - deviations * deviation,
    }


def atr(highs, lows, closes, period=14) -> float:
    high, low, close = [_series(v, period + 1) for v in (highs, lows, closes)]
    if not (len(high) == len(low) == len(close)) or (high < low).any():
        raise ValueError("Invalid OHLC arrays")
    ranges = np.maximum(
        high[1:] - low[1:], np.maximum(abs(high[1:] - close[:-1]), abs(low[1:] - close[:-1]))
    )
    result = float(ranges[:period].mean())
    for value in ranges[period:]:
        result = (result * (period - 1) + value) / period
    return float(result)


def safe_ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    if not numerator.is_finite() or not denominator.is_finite() or denominator == 0:
        raise ValueError("Ratio requires finite values and nonzero denominator")
    return numerator / denominator


def reward_risk(entry: Decimal, stop: Decimal, target: Decimal) -> Decimal:
    if not all(v.is_finite() for v in (entry, stop, target)) or not 0 < stop < entry < target:
        raise ValueError("Long plan requires 0 < stop < entry < target")
    return (target - entry) / (entry - stop)


def position_size(
    value: Decimal,
    risk: Decimal,
    entry: Decimal,
    stop: Decimal,
    buying_power: Decimal,
    max_weight: Decimal,
    existing_value=Decimal(0),
    fee=Decimal(0),
    slippage=Decimal(0),
    tax=Decimal(0),
    lot_step=Decimal(1),
) -> Decimal:
    """Maximum quantity, floored to the broker's tradable quantity step."""
    numbers = (
        value,
        risk,
        entry,
        stop,
        buying_power,
        max_weight,
        existing_value,
        fee,
        slippage,
        tax,
    )
    if not all(v.is_finite() and v >= 0 for v in numbers):
        raise ValueError("Sizing inputs must be finite and nonnegative")
    if not value > 0 or not 0 < risk <= 1 or not 0 < max_weight <= 1 or not 0 < stop < entry:
        raise ValueError("Invalid sizing constraints")
    if not lot_step.is_finite() or not lot_step > 0:
        raise ValueError("Quantity step must be positive")
    cost = entry * (1 + fee + slippage)
    loss = cost - stop * (1 - fee - slippage - tax)
    limit = min(
        value * risk / loss,
        buying_power / cost,
        max(Decimal(0), value * max_weight - existing_value) / cost,
    )
    return (limit / lot_step).to_integral_value(rounding=ROUND_FLOOR) * lot_step


def technical_snapshot(candles: list[dict]) -> dict:
    if len(candles) < 2:
        raise ValueError("Insufficient OHLCV")
    close = [float(bar["close"]) for bar in candles]
    high = [float(bar["high"]) for bar in candles]
    low = [float(bar["low"]) for bar in candles]
    volume = [float(bar["volume"]) for bar in candles]
    result = {"bars": len(close), "last_session": candles[-1]["timestamp"]}
    if len(close) >= 10:
        result["ema10"] = ema(close, 10)
    for period in (50, 200):
        if len(close) >= period:
            result[f"sma{period}"] = sma(close, period)
    if len(close) >= 15:
        result["rsi14"], result["atr14"] = rsi(close), atr(high, low, close)
    if len(close) >= 34:
        result.update(macd(close))
    if len(close) >= 20:
        result.update({f"bb_{key}": val for key, val in bollinger(close).items()})
        result["volume_ratio20"] = volume[-1] / sma(volume, 20) if sma(volume, 20) else None
    # Observable extremes, not an assertion that a price is a support/resistance.
    window = candles[-20:]
    result["recent_low"] = min(window, key=lambda bar: Decimal(str(bar["low"])))
    result["recent_high"] = max(window, key=lambda bar: Decimal(str(bar["high"])))
    year = candles[-252:]  # up to 52 weeks of completed sessions
    result["low_52w"] = min(year, key=lambda bar: Decimal(str(bar["low"])))
    result["high_52w"] = max(year, key=lambda bar: Decimal(str(bar["high"])))
    return result
