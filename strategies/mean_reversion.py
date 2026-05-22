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

CHART_CONFIG = {
    "overlays": ["bb"],
    "signals": True,
    "extra_cols": ["support", "resistance", "stop_px", "target_px", "structure_rr"],
}


def _nearest_prior_levels(frame: pd.DataFrame, lookback: int = 72) -> tuple[pd.Series, pd.Series]:
    support = pd.Series(np.nan, index=frame.index, dtype=float)
    resistance = pd.Series(np.nan, index=frame.index, dtype=float)
    for i, (_, row) in enumerate(frame.iterrows()):
        if i < 2:
            continue
        close = float(row["Close"])
        atr = float(row.get("atr", np.nan))
        if not np.isfinite(close):
            continue
        min_gap = max(abs(close) * 0.0005, atr * 0.5 if np.isfinite(atr) else 0.0, 1e-6)
        window = frame.iloc[max(0, i - lookback):i]
        lows = window["Low"].dropna()
        highs = window["High"].dropna()
        support_candidates = lows[lows < close - min_gap]
        resistance_candidates = highs[highs > close + min_gap]
        if not support_candidates.empty:
            support.iloc[i] = float(support_candidates.max())
        if not resistance_candidates.empty:
            resistance.iloc[i] = float(resistance_candidates.min())
    return support, resistance


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

    support, resistance = _nearest_prior_levels(frame)
    frame["support"] = support
    frame["resistance"] = resistance

    long_risk = frame["Close"] - support
    long_reward = resistance - frame["Close"]
    short_risk = resistance - frame["Close"]
    short_reward = frame["Close"] - support
    long_structure_ok = (long_risk > 0) & (long_reward > long_risk)
    short_structure_ok = (short_risk > 0) & (short_reward > short_risk)

    long_setup = (frame["Close"] < lo) & (frame["rsi"] < 35) & long_structure_ok
    short_setup = (frame["Close"] > hi) & (frame["rsi"] > 65) & short_structure_ok
    frame.loc[long_setup, "entry_side"] = LONG
    frame.loc[short_setup, "entry_side"] = SHORT
    frame.loc[long_setup, "stop_px"] = support[long_setup]
    frame.loc[long_setup, "target_px"] = resistance[long_setup]
    frame.loc[long_setup, "structure_rr"] = long_reward[long_setup] / long_risk[long_setup]
    frame.loc[short_setup, "stop_px"] = resistance[short_setup]
    frame.loc[short_setup, "target_px"] = support[short_setup]
    frame.loc[short_setup, "structure_rr"] = short_reward[short_setup] / short_risk[short_setup]
    frame["exit_long"] = (frame["Close"] >= mid) | (frame["rsi"] > 52)
    frame["exit_short"] = (frame["Close"] <= mid) | (frame["rsi"] < 48)
    frame["reason"] = "Enter Bollinger/RSI extremes only when nearest structure target is greater than stop risk."
    return finalize(META, frame, ctx)
