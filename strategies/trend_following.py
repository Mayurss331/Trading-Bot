from __future__ import annotations

import numpy as np
import pandas as pd

from .base import LONG, SHORT, StrategyContext, StrategyMeta, base_frame, ema, finalize


META = StrategyMeta(
    id="trend_following",
    name="Trend Following",
    description="Follows directional momentum using EMA alignment and Supertrend confirmation.",
    chart_label="Supertrend",
    score_label="Trend Score",
)

CHART_CONFIG = {
    "overlays": ["ema", "supertrend"],
    "signals": True,
    "extra_cols": [],
}


def analyze(bars: pd.DataFrame, ctx: StrategyContext) -> dict:
    frame = base_frame(bars)
    frame["ema_fast"] = ema(frame["Close"], 9)
    frame["ema_slow"] = ema(frame["Close"], 21)
    frame["ema_trend"] = ema(frame["Close"], 55)
    slope = frame["ema_fast"].diff(3)

    bull = (frame["ema_fast"] > frame["ema_slow"]) & (frame["Close"] > frame["st_line"]) & (frame["Close"] > frame["ema_trend"])
    bear = (frame["ema_fast"] < frame["ema_slow"]) & (frame["Close"] < frame["st_line"]) & (frame["Close"] < frame["ema_trend"])
    strong_bull = bull & (slope > 0) & (frame["rsi"] > 50)
    strong_bear = bear & (slope < 0) & (frame["rsi"] < 50)

    score = pd.Series(0, index=frame.index)
    score += np.where(frame["ema_fast"] > frame["ema_slow"], 2, -2)
    score += np.where(frame["Close"] > frame["st_line"], 2, -2)
    score += np.where(frame["Close"] > frame["ema_trend"], 1, -1)
    score += np.where(frame["rsi"] > 55, 1, np.where(frame["rsi"] < 45, -1, 0))
    frame["score"] = score

    frame.loc[strong_bull, "entry_side"] = LONG
    frame.loc[strong_bear, "entry_side"] = SHORT
    frame["exit_long"] = (frame["ema_fast"] < frame["ema_slow"]) | (frame["Close"] < frame["st_line"]) | (frame["rsi"] < 45)
    frame["exit_short"] = (frame["ema_fast"] > frame["ema_slow"]) | (frame["Close"] > frame["st_line"]) | (frame["rsi"] > 55)
    frame["reason"] = "EMA 9/21, EMA 55, Supertrend, and RSI must align with momentum."
    return finalize(META, frame, ctx)
