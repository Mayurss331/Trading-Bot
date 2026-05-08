"""Volatility Squeeze Breakout — directional strategy.

Captures short-term breakouts after low-volatility squeezes using
Bollinger Bands, RSI, and Supertrend confirmation.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .base import LONG, SHORT, StrategyContext, StrategyMeta, base_frame, bollinger, finalize


META = StrategyMeta(
    id="volatility_squeeze",
    name="Volatility Squeeze",
    description="Captures breakouts after low-volatility Bollinger Band squeezes with RSI confirmation.",
    chart_label="Bollinger Mid",
    score_label="Squeeze Score",
)

MIN_BARS = 120


def analyze(bars: pd.DataFrame, ctx: StrategyContext) -> dict:
    if len(bars) < MIN_BARS:
        frame = base_frame(bars)
        frame["reason"] = "Insufficient bars for volatility squeeze analysis."
        return finalize(META, frame, ctx, notes=[f"Need {MIN_BARS} bars, got {len(bars)}."])

    frame = base_frame(bars)

    # Bollinger Bands (base_frame does NOT compute these)
    lo, mid, hi = bollinger(frame)
    frame["bb_lower"] = lo
    frame["bb_mid"] = mid
    frame["bb_upper"] = hi
    frame["st_line"] = mid  # chart overlay uses mid band

    # Band width and squeeze detection
    band_width = (hi - lo) / mid.replace(0, np.nan)
    squeeze_pct = band_width.rolling(MIN_BARS, min_periods=MIN_BARS).quantile(0.20)
    squeeze_flag = (band_width <= squeeze_pct).astype(int)

    # Rolling 5-bar window: squeeze active if any of last 5 bars in squeeze
    squeeze_active = squeeze_flag.rolling(5, min_periods=1).max().astype(bool)

    rsi = frame["rsi"]

    # Entry conditions
    long_entry = squeeze_active & (frame["Close"] > hi) & (rsi > 55)
    short_entry = squeeze_active & (frame["Close"] < lo) & (rsi < 45)

    # Exit conditions
    exit_long = (frame["Close"] <= mid) | (rsi < 50)
    exit_short = (frame["Close"] >= mid) | (rsi > 50)

    frame.loc[long_entry, "entry_side"] = LONG
    frame.loc[short_entry, "entry_side"] = SHORT
    frame["exit_long"] = exit_long
    frame["exit_short"] = exit_short

    # Score: sign * squeeze_strength (1=squeeze, 2=breakout)
    breakout = (frame["Close"] > hi) | (frame["Close"] < lo)
    squeeze_strength = np.where(breakout & squeeze_active, 2, np.where(squeeze_active, 1, 0))
    sign = np.where(long_entry, 1, np.where(short_entry, -1, 0))
    frame["score"] = sign * squeeze_strength

    # Reason per bar
    reason = pd.Series("", index=frame.index)
    reason[squeeze_active & ~breakout] = "squeeze"
    reason[long_entry] = "breakout_long"
    reason[short_entry] = "breakout_short"
    reason[exit_long & ~long_entry] = "exit"
    reason[exit_short & ~short_entry] = "exit"
    frame["reason"] = reason.where(reason != "", "Score >= +3 enters long, score <= -3 enters short; weaker score exits.")

    return finalize(META, frame, ctx)
