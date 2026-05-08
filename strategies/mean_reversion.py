from __future__ import annotations

import numpy as np
import pandas as pd

from .base import LONG, SHORT, StrategyContext, StrategyMeta, base_frame, bollinger, finalize


META = StrategyMeta(
    id="mean_reversion",
    name="Mean Reversion",
    description="Looks for stretched price moves outside Bollinger Bands with RSI extremes.",
    chart_label="Bollinger Mid",
    score_label="Reversion Score",
)


def analyze(bars: pd.DataFrame, ctx: StrategyContext) -> dict:
    frame = base_frame(bars)
    lo, mid, hi = bollinger(frame)
    frame["bb_lower"] = lo
    frame["bb_mid"] = mid
    frame["bb_upper"] = hi
    frame["st_line"] = mid

    band_width = (hi - lo).replace(0, np.nan)
    z = ((frame["Close"] - mid) / band_width).clip(-2, 2)
    score = pd.Series(0, index=frame.index)
    score += np.where(frame["Close"] < lo, 3, 0)
    score += np.where(frame["rsi"] < 35, 2, 0)
    score -= np.where(frame["Close"] > hi, 3, 0)
    score -= np.where(frame["rsi"] > 65, 2, 0)
    score += np.where(z < -0.35, 1, np.where(z > 0.35, -1, 0))
    frame["score"] = score

    long_setup = (frame["Close"] < lo) & (frame["rsi"] < 35)
    short_setup = (frame["Close"] > hi) & (frame["rsi"] > 65)
    frame.loc[long_setup, "entry_side"] = LONG
    frame.loc[short_setup, "entry_side"] = SHORT
    frame["exit_long"] = (frame["Close"] >= mid) | (frame["rsi"] > 52)
    frame["exit_short"] = (frame["Close"] <= mid) | (frame["rsi"] < 48)
    frame["reason"] = "Enter against stretched Bollinger/RSI extremes; exit near the mean."
    return finalize(META, frame, ctx)
