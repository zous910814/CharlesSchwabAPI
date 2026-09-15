from __future__ import annotations

from typing import Optional


def compute_kd(candles: list[dict], k_period: int = 14, d_period: int = 5) -> list[dict]:
    """Add 'k' and 'd' (stochastic %K/%D) to each candle dict, in place.

    Candles must be sorted oldest-to-newest with 'high'/'low'/'close' keys
    (Schwab's pricehistory candle shape). Candles without a full k_period
    lookback get k=None; d needs d_period consecutive k values.
    """
    highs = [c.get("high") for c in candles]
    lows = [c.get("low") for c in candles]
    closes = [c.get("close") for c in candles]

    k_values: list[Optional[float]] = []
    for i in range(len(candles)):
        if i < k_period - 1:
            k_values.append(None)
            continue
        window_high = max(highs[i - k_period + 1 : i + 1])
        window_low = min(lows[i - k_period + 1 : i + 1])
        if window_high is None or window_low is None or window_high == window_low:
            k_values.append(None)
        else:
            k_values.append(100 * (closes[i] - window_low) / (window_high - window_low))

    for i, candle in enumerate(candles):
        window = [v for v in k_values[max(0, i - d_period + 1) : i + 1] if v is not None]
        candle["k"] = k_values[i]
        candle["d"] = sum(window) / len(window) if len(window) == d_period else None

    return candles


def kd_signal(candles: list[dict], oversold: float = 20.0, overbought: float = 80.0) -> str:
    """Classic stochastic K/D crossover: 'BUY' when K crosses above D while
    oversold, 'SELL' when K crosses below D while overbought, else 'HOLD'.

    Candles must already carry 'k'/'d' (see compute_kd), sorted oldest-to-newest.
    """
    valid = [c for c in candles if c.get("k") is not None and c.get("d") is not None]
    if len(valid) < 2:
        return "HOLD"
    prev, curr = valid[-2], valid[-1]
    if prev["k"] <= prev["d"] and curr["k"] > curr["d"] and curr["k"] < oversold:
        return "BUY"
    if prev["k"] >= prev["d"] and curr["k"] < curr["d"] and curr["k"] > overbought:
        return "SELL"
    return "HOLD"
