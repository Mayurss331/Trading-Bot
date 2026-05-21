from __future__ import annotations

import numpy as np
import pandas as pd

from .base import LONG, SHORT, StrategyContext, StrategyMeta, base_frame, finalize


META = StrategyMeta(
    id="daily_sweep",
    name="Daily Sweep",
    description="1H BOS bias, 5m liquidity sweep + CHoCH, enter on FVG; target prior day H/L.",
    chart_label="Daily Sweep",
    score_label="Bias",
)


CHART_CONFIG = {
    "overlays": ["sweep"],
    "signals": False,
    "extra_cols": [],
}

PIVOT_LEFT = 2
PIVOT_RIGHT = 2
RETRACE_ATR = 0.5
SWEEP_ATR = 0.05
STOP_ATR = 0.1
CHOCH_WINDOW = 6
MIN_FVG_ATR = 0.05
TP1_FRAC = 0.5


def _resample_ohlc(bars: pd.DataFrame, rule: str) -> pd.DataFrame:
    ohlc = (
        bars.resample(rule, label="right", closed="left")
        .agg(
            Open=("Open", "first"),
            High=("High", "max"),
            Low=("Low", "min"),
            Close=("Close", "last"),
            Volume=("Volume", "sum"),
        )
        .dropna(subset=["Open", "High", "Low", "Close"])
    )
    return ohlc


def _confirmed_pivot_levels(high: pd.Series, low: pd.Series, left: int, right: int) -> tuple[pd.Series, pd.Series]:
    n = len(high)
    swing_high = np.full(n, np.nan)
    swing_low = np.full(n, np.nan)
    high_values = high.to_numpy()
    low_values = low.to_numpy()
    for i in range(left, n - right):
        hi = high_values[i]
        lo = low_values[i]
        if not np.isfinite(hi) or not np.isfinite(lo):
            continue
        window_high = high_values[i - left : i + right + 1]
        window_low = low_values[i - left : i + right + 1]
        confirm_i = i + right
        if np.nanmax(window_high) == hi and hi > np.nanmax(high_values[i - left : i]):
            swing_high[confirm_i] = hi
        if np.nanmin(window_low) == lo and lo < np.nanmin(low_values[i - left : i]):
            swing_low[confirm_i] = lo
    return pd.Series(swing_high, index=high.index), pd.Series(swing_low, index=low.index)


def analyze(bars: pd.DataFrame, ctx: StrategyContext) -> dict:
    frame = base_frame(bars)
    if frame.empty:
        frame["reason"] = "Waiting for bars to build Daily Sweep context."
        return finalize(META, frame, ctx, notes=["No bars available"])

    hourly = _resample_ohlc(bars, "1h")
    daily = _resample_ohlc(bars, "1D")
    if hourly.empty or daily.empty:
        frame["reason"] = "Waiting for enough 1H/1D bars to build Daily Sweep context."
        return finalize(META, frame, ctx, notes=["Insufficient higher timeframe data"])

    h_swing_high, h_swing_low = _confirmed_pivot_levels(hourly["High"], hourly["Low"], PIVOT_LEFT, PIVOT_RIGHT)
    hourly["swing_high"] = h_swing_high
    hourly["swing_low"] = h_swing_low
    hourly["last_swing_high"] = hourly["swing_high"].ffill()
    hourly["last_swing_low"] = hourly["swing_low"].ffill()

    hourly["bos"] = 0
    long_bos = hourly["last_swing_high"].shift(1).notna() & (hourly["Close"] > hourly["last_swing_high"].shift(1))
    short_bos = hourly["last_swing_low"].shift(1).notna() & (hourly["Close"] < hourly["last_swing_low"].shift(1))
    hourly.loc[long_bos, "bos"] = LONG
    hourly.loc[short_bos, "bos"] = SHORT
    hourly["bias"] = hourly["bos"].replace(0, np.nan).ffill().fillna(0)

    frame["bias"] = hourly["bias"].reindex(frame.index, method="ffill").fillna(0).astype(int)
    frame["score"] = frame["bias"]

    daily_levels = daily[["High", "Low"]].rename(columns={"High": "prev_day_high", "Low": "prev_day_low"})
    frame = frame.join(daily_levels.reindex(frame.index, method="ffill"))

    f_swing_high, f_swing_low = _confirmed_pivot_levels(frame["High"], frame["Low"], PIVOT_LEFT, PIVOT_RIGHT)
    frame["swing_high"] = f_swing_high
    frame["swing_low"] = f_swing_low
    frame["last_swing_high"] = frame["swing_high"].ffill()
    frame["last_swing_low"] = frame["swing_low"].ffill()

    bias_change = (frame["bias"] != frame["bias"].shift()) & (frame["bias"] != 0)
    frame["bos"] = frame["bias"].where(bias_change, 0).astype(int)
    frame["bias_group"] = bias_change.cumsum().where(frame["bias"] != 0)
    frame["bias_high"] = frame.groupby("bias_group")["High"].cummax()
    frame["bias_low"] = frame.groupby("bias_group")["Low"].cummin()

    atr = frame["atr"].fillna(0.0)
    retrace_long = (frame["bias"] == LONG) & (frame["Close"] <= frame["bias_high"] - RETRACE_ATR * atr)
    retrace_short = (frame["bias"] == SHORT) & (frame["Close"] >= frame["bias_low"] + RETRACE_ATR * atr)
    frame["retracement"] = retrace_long | retrace_short

    sweep_buffer = np.maximum(atr * SWEEP_ATR, frame["Close"] * 0.0002)
    sweep_long = (
        frame["retracement"]
        & (frame["bias"] == LONG)
        & frame["last_swing_low"].notna()
        & (frame["Low"] < frame["last_swing_low"] - sweep_buffer)
        & (frame["Close"] > frame["last_swing_low"])
    )
    sweep_short = (
        frame["retracement"]
        & (frame["bias"] == SHORT)
        & frame["last_swing_high"].notna()
        & (frame["High"] > frame["last_swing_high"] + sweep_buffer)
        & (frame["Close"] < frame["last_swing_high"])
    )

    n = len(frame)
    entry_side = np.zeros(n, dtype=int)
    stop_px = np.full(n, np.nan)
    tp1_px = np.full(n, np.nan)
    tp2_px = np.full(n, np.nan)
    tp1_frac = np.full(n, np.nan)
    fvg_low_arr = np.full(n, np.nan)
    fvg_high_arr = np.full(n, np.nan)
    phase_arr = np.full(n, "neutral", dtype=object)
    sweep_arr = np.zeros(n, dtype=bool)
    choch_arr = np.zeros(n, dtype=bool)

    pending_dir = 0
    sweep_idx = -1
    sweep_extreme = np.nan
    choch_idx = -1
    fvg_dir = 0
    fvg_low = np.nan
    fvg_high = np.nan
    fvg_idx = -1

    highs = frame["High"].to_numpy()
    lows = frame["Low"].to_numpy()
    closes = frame["Close"].to_numpy()
    last_high = frame["last_swing_high"].to_numpy()
    last_low = frame["last_swing_low"].to_numpy()
    bias = frame["bias"].to_numpy()
    prev_high = frame["prev_day_high"].to_numpy()
    prev_low = frame["prev_day_low"].to_numpy()

    for i in range(2, n):
        if bias[i] == 0:
            pending_dir = 0
            fvg_dir = 0
            choch_idx = -1
            phase_arr[i] = "neutral"
            continue

        if sweep_long.iloc[i]:
            pending_dir = LONG
            sweep_idx = i
            sweep_extreme = lows[i]
            choch_idx = -1
            fvg_dir = 0
            sweep_arr[i] = True
        elif sweep_short.iloc[i]:
            pending_dir = SHORT
            sweep_idx = i
            sweep_extreme = highs[i]
            choch_idx = -1
            fvg_dir = 0
            sweep_arr[i] = True

        if pending_dir == 0:
            phase_arr[i] = "accumulation"
            continue
        if i - sweep_idx > CHOCH_WINDOW:
            pending_dir = 0
            fvg_dir = 0
            choch_idx = -1
            phase_arr[i] = "accumulation"
            continue

        prev_choch = choch_idx
        if pending_dir == LONG and np.isfinite(last_high[i]) and closes[i] > last_high[i]:
            choch_idx = i
        elif pending_dir == SHORT and np.isfinite(last_low[i]) and closes[i] < last_low[i]:
            choch_idx = i
        if choch_idx != prev_choch and choch_idx == i:
            choch_arr[i] = True

        if choch_idx >= 0:
            if pending_dir == LONG and highs[i - 2] < lows[i]:
                gap = lows[i] - highs[i - 2]
                if gap >= atr.iloc[i] * MIN_FVG_ATR:
                    fvg_dir = LONG
                    fvg_low = highs[i - 2]
                    fvg_high = lows[i]
                    fvg_idx = i
            elif pending_dir == SHORT and lows[i - 2] > highs[i]:
                gap = lows[i - 2] - highs[i]
                if gap >= atr.iloc[i] * MIN_FVG_ATR:
                    fvg_dir = SHORT
                    fvg_low = highs[i]
                    fvg_high = lows[i - 2]
                    fvg_idx = i

        if fvg_dir != 0:
            fvg_low_arr[i] = fvg_low
            fvg_high_arr[i] = fvg_high

        # Phase assignment
        if fvg_dir != 0:
            phase_arr[i] = "distribution"
        elif choch_idx >= 0:
            phase_arr[i] = "distribution"
        else:
            phase_arr[i] = "manipulation"

        if fvg_dir != 0 and i > fvg_idx:
            touches_gap = lows[i] <= fvg_high and highs[i] >= fvg_low
            if fvg_dir == LONG and touches_gap:
                entry = closes[i]
                stop_buffer = max(atr.iloc[i] * STOP_ATR, entry * 0.0005)
                stop = sweep_extreme - stop_buffer
                target = prev_high[i]
                risk = entry - stop
                if np.isfinite(target) and target > entry and risk > 0:
                    entry_side[i] = LONG
                    stop_px[i] = stop
                    tp1_px[i] = entry + risk
                    tp2_px[i] = target
                    tp1_frac[i] = TP1_FRAC
                    pending_dir = 0
                    fvg_dir = 0
                    choch_idx = -1
            elif fvg_dir == SHORT and touches_gap:
                entry = closes[i]
                stop_buffer = max(atr.iloc[i] * STOP_ATR, entry * 0.0005)
                stop = sweep_extreme + stop_buffer
                target = prev_low[i]
                risk = stop - entry
                if np.isfinite(target) and target < entry and risk > 0:
                    entry_side[i] = SHORT
                    stop_px[i] = stop
                    tp1_px[i] = entry - risk
                    tp2_px[i] = target
                    tp1_frac[i] = TP1_FRAC
                    pending_dir = 0
                    fvg_dir = 0
                    choch_idx = -1

    frame["entry_side"] = entry_side
    frame["stop_px"] = stop_px
    frame["tp1_px"] = tp1_px
    frame["tp2_px"] = tp2_px
    frame["tp1_frac"] = tp1_frac
    frame["fvg_low"] = fvg_low_arr
    frame["fvg_high"] = fvg_high_arr
    frame["phase"] = phase_arr
    frame["sweep"] = sweep_arr
    frame["choch"] = choch_arr
    frame["exit_long"] = frame["bias"] == SHORT
    frame["exit_short"] = frame["bias"] == LONG
    frame["st_line"] = np.nan
    frame["reason"] = "1H BOS bias; 5m sweep + CHoCH; enter on 3-candle FVG tap; TP1=1R, TP2=prior IST day H/L."

    notes = []
    median_delta = frame.index.to_series().diff().median()
    if isinstance(median_delta, pd.Timedelta):
        minutes = median_delta.total_seconds() / 60
        if minutes > 7:
            notes.append(f"Best with 5m bars; current median {minutes:.1f}m")

    latest = frame.iloc[-1]
    bias_val = int(latest.get("bias", 0) or 0)
    bias_label = "LONG" if bias_val == LONG else "SHORT" if bias_val == SHORT else "NEUTRAL"
    notes.append(f"1H bias: {bias_label}")

    def _finite(v):
        try:
            f = float(v)
            return f if np.isfinite(f) else None
        except (TypeError, ValueError):
            return None

    extra = {
        "bias": bias_val,
        "bias_label": bias_label,
        "phase": str(latest.get("phase") or "neutral"),
        "fvg_low": _finite(latest.get("fvg_low")),
        "fvg_high": _finite(latest.get("fvg_high")),
        "prev_day_high": _finite(latest.get("prev_day_high")),
        "prev_day_low": _finite(latest.get("prev_day_low")),
    }
    return finalize(META, frame, ctx, notes=notes, extra_indicators=extra)
