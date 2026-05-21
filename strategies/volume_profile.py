"""Volume Profile strategy.

Rolling fixed-range profile inspired by TradingView's Fixed Range Volume Profile:
POC is the highest-volume price row, and VAH/VAL bound the configured value area.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .base import LONG, SHORT, StrategyContext, StrategyMeta, atr_s, base_frame, finalize, risk_per_unit


META = StrategyMeta(
    id="volume_profile",
    name="Volume Profile",
    description="Rolling fixed-range volume profile using POC, VAH, and VAL as context support/resistance.",
    chart_label="POC / Value Area",
    score_label="VP Score",
)

CHART_CONFIG = {
    "overlays": ["volume_profile"],
    "signals": True,
    "extra_cols": ["vp_poc", "vp_vah", "vp_val", "vp_position", "volume_ratio"],
}

PROFILE_LOOKBACK = 240
PROFILE_ROWS = 120
VALUE_AREA_PCT = 0.72
MIN_BARS = PROFILE_LOOKBACK + 5
ATR_BUFFER = 0.35
VOL_CONFIRM = 1.05


def _value_area_from_hist(hist: np.ndarray, edges: np.ndarray, pct: float) -> tuple[float, float, float]:
    total = float(hist.sum())
    if total <= 0:
        return np.nan, np.nan, np.nan

    poc_idx = int(np.argmax(hist))
    target = total * max(0.01, min(float(pct), 0.99))
    included = hist[poc_idx]
    low_idx = high_idx = poc_idx

    while included < target and (low_idx > 0 or high_idx < len(hist) - 1):
        left_vol = hist[low_idx - 1] if low_idx > 0 else -1.0
        right_vol = hist[high_idx + 1] if high_idx < len(hist) - 1 else -1.0
        if right_vol >= left_vol and high_idx < len(hist) - 1:
            high_idx += 1
            included += hist[high_idx]
        elif low_idx > 0:
            low_idx -= 1
            included += hist[low_idx]
        else:
            break

    poc = float((edges[poc_idx] + edges[poc_idx + 1]) / 2)
    val = float(edges[low_idx])
    vah = float(edges[high_idx + 1])
    return poc, vah, val


def _rolling_profile(df: pd.DataFrame, lookback: int, rows: int, value_area_pct: float) -> pd.DataFrame:
    n = len(df)
    poc = np.full(n, np.nan)
    vah = np.full(n, np.nan)
    val = np.full(n, np.nan)

    typical = ((df["High"] + df["Low"] + df["Close"]) / 3).to_numpy(dtype=float)
    volume = df["Volume"].fillna(0).clip(lower=0).to_numpy(dtype=float)
    lows = df["Low"].to_numpy(dtype=float)
    highs = df["High"].to_numpy(dtype=float)

    for i in range(lookback - 1, n):
        start = i - lookback + 1
        lo = float(np.nanmin(lows[start : i + 1]))
        hi = float(np.nanmax(highs[start : i + 1]))
        if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
            continue
        hist, edges = np.histogram(
            typical[start : i + 1],
            bins=rows,
            range=(lo, hi),
            weights=volume[start : i + 1],
        )
        poc[i], vah[i], val[i] = _value_area_from_hist(hist.astype(float), edges.astype(float), value_area_pct)

    return pd.DataFrame({"vp_poc": poc, "vp_vah": vah, "vp_val": val}, index=df.index)


def _position_label(close: pd.Series, val: pd.Series, poc: pd.Series, vah: pd.Series) -> pd.Series:
    labels = pd.Series("warming", index=close.index, dtype=object)
    labels[(val.notna()) & (close < val)] = "below_value"
    labels[(val.notna()) & (close >= val) & (close < poc)] = "lower_value"
    labels[(poc.notna()) & (close >= poc) & (close <= vah)] = "upper_value"
    labels[(vah.notna()) & (close > vah)] = "above_value"
    return labels


def analyze(bars: pd.DataFrame, ctx: StrategyContext) -> dict:
    frame = base_frame(bars)
    if len(frame) < MIN_BARS:
        frame["reason"] = f"Need {MIN_BARS} bars for Volume Profile warmup."
        return finalize(META, frame, ctx, notes=[f"Need {MIN_BARS} bars, got {len(frame)}."])

    profile = _rolling_profile(frame, PROFILE_LOOKBACK, PROFILE_ROWS, VALUE_AREA_PCT)
    frame = frame.join(profile)
    frame["st_line"] = frame["vp_poc"]
    frame["atr"] = atr_s(frame)

    close = frame["Close"]
    open_ = frame["Open"]
    high = frame["High"]
    low = frame["Low"]
    atr = frame["atr"].fillna(close * 0.005)
    poc = frame["vp_poc"]
    vah = frame["vp_vah"]
    val = frame["vp_val"]
    buffer = np.maximum(atr * ATR_BUFFER, close * 0.001)

    vol_avg = frame["Volume"].rolling(20).mean().replace(0, np.nan)
    frame["volume_ratio"] = frame["Volume"] / vol_avg
    vol_ok = frame["volume_ratio"].fillna(0) >= VOL_CONFIRM

    val_reclaim = (low <= val + buffer) & (close > val) & (close > open_) & (close.shift(1) <= val.shift(1))
    vah_reject = (high >= vah - buffer) & (close < vah) & (close < open_) & (close.shift(1) >= vah.shift(1))
    vah_breakout = (close > vah) & (close.shift(1) <= vah.shift(1)) & vol_ok
    val_breakdown = (close < val) & (close.shift(1) >= val.shift(1)) & vol_ok

    long_entry = val_reclaim | vah_breakout
    short_entry = (vah_reject | val_breakdown) & ctx.allow_shorts

    frame.loc[long_entry, "entry_side"] = LONG
    frame.loc[short_entry, "entry_side"] = SHORT

    frame["stop_px"] = np.nan
    frame["tp2_px"] = np.nan

    long_stop = np.where(vah_breakout, np.minimum(vah, poc) - buffer, val - buffer)
    short_stop = np.where(val_breakdown, np.maximum(val, poc) + buffer, vah + buffer)
    long_risk = pd.Series([risk_per_unit(e, s) for e, s in zip(close, long_stop)], index=frame.index)
    short_risk = pd.Series([risk_per_unit(e, s) for e, s in zip(close, short_stop)], index=frame.index)

    long_target = np.where(val_reclaim & (poc > close), poc, close + 2 * long_risk)
    long_target = np.where((long_target <= close) | ~np.isfinite(long_target), close + 2 * long_risk, long_target)
    short_target = np.where(vah_reject & (poc < close), poc, close - 2 * short_risk)
    short_target = np.where((short_target >= close) | ~np.isfinite(short_target), close - 2 * short_risk, short_target)

    frame.loc[long_entry, "stop_px"] = pd.Series(long_stop, index=frame.index)[long_entry]
    frame.loc[long_entry, "tp2_px"] = pd.Series(long_target, index=frame.index)[long_entry]
    frame.loc[short_entry, "stop_px"] = pd.Series(short_stop, index=frame.index)[short_entry]
    frame.loc[short_entry, "tp2_px"] = pd.Series(short_target, index=frame.index)[short_entry]

    score = pd.Series(0, index=frame.index, dtype=int)
    score[val_reclaim] = 3
    score[vah_breakout] = 4
    score[vah_reject] = -3
    score[val_breakdown] = -4
    frame["score"] = score
    frame["vp_position"] = _position_label(close, val, poc, vah)

    frame["exit_long"] = (close < poc) | (close < val)
    frame["exit_short"] = (close > poc) | (close > vah)

    reason = pd.Series("POC is context; VAH/VAL act as value-area resistance/support.", index=frame.index, dtype=object)
    reason[val_reclaim] = "VAL reclaim: value-area low acted as support."
    reason[vah_breakout] = "VAH breakout with volume confirmation."
    reason[vah_reject] = "VAH rejection: value-area high acted as resistance."
    reason[val_breakdown] = "VAL breakdown with volume confirmation."
    frame["reason"] = reason

    latest = frame.iloc[-1]

    def _finite(value: object) -> float | None:
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        return f if np.isfinite(f) else None

    notes = [
        f"Profile rows={PROFILE_ROWS}, value area={VALUE_AREA_PCT:.0%}, lookback={PROFILE_LOOKBACK} bars.",
        "POC marks the highest-volume row; VAH/VAL mark the 72% value area.",
    ]
    return finalize(
        META,
        frame,
        ctx,
        notes=notes,
        extra_indicators={
            "vp_poc": _finite(latest.get("vp_poc")),
            "vp_vah": _finite(latest.get("vp_vah")),
            "vp_val": _finite(latest.get("vp_val")),
            "vp_position": str(latest.get("vp_position") or "warming"),
            "profile_rows": PROFILE_ROWS,
            "value_area_pct": VALUE_AREA_PCT * 100,
        },
    )
