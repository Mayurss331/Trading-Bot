from __future__ import annotations

import pandas as pd

from .base import LONG, SHORT, StrategyContext, StrategyMeta, base_frame, bollinger, ema, finalize, macd_hist


META = StrategyMeta(
    id="confluence",
    name="Confluence",
    description="Supertrend, EMA/RSI, MACD, Bollinger, and RSI vote together.",
    chart_label="Supertrend",
    score_label="Confluence Score",
)

CHART_CONFIG = {
    "overlays": ["ema", "bb", "supertrend"],
    "signals": True,
    "extra_cols": [],
}


def analyze(bars: pd.DataFrame, ctx: StrategyContext) -> dict:
    frame = base_frame(bars)

    fast = ema(frame["Close"], 9)
    slow = ema(frame["Close"], 21)
    rsi = frame["rsi"]
    hist = macd_hist(frame["Close"])
    lo, mid, hi = bollinger(frame)

    v_er = pd.Series(0, index=frame.index)
    v_er[(fast > slow) & (rsi > 45)] = 1
    v_er[(fast < slow) & (rsi < 55)] = -1

    v_mc = pd.Series(0, index=frame.index)
    v_mc[hist > 0] = 1
    v_mc[hist < 0] = -1

    v_bb = pd.Series(0, index=frame.index)
    v_bb[frame["Close"] > mid] = 1
    v_bb[frame["Close"] < mid] = -1
    v_bb[frame["Close"] > hi] = 1
    v_bb[frame["Close"] < lo] = -1

    v_rs = pd.Series(0, index=frame.index)
    v_rs[(rsi > 50) & (rsi < 70)] = 1
    v_rs[(rsi < 50) & (rsi > 30)] = -1
    v_rs[rsi >= 70] = 0
    v_rs[rsi <= 30] = 0

    frame["ema_fast"] = fast
    frame["ema_slow"] = slow
    frame["bb_lower"] = lo
    frame["bb_mid"] = mid
    frame["bb_upper"] = hi
    frame["score"] = frame["st_dir"] + v_er + v_mc + v_bb + v_rs
    frame.loc[frame["score"] >= 3, "entry_side"] = LONG
    frame.loc[frame["score"] <= -3, "entry_side"] = SHORT
    frame["exit_long"] = frame["score"] < 2
    frame["exit_short"] = frame["score"] > -2
    frame["reason"] = "Score >= +3 enters long, score <= -3 enters short; weaker score exits."
    return finalize(META, frame, ctx)
