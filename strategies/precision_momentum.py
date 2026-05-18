"""Precision Momentum — High-accuracy multi-layer trend strategy.

Entry requires ≥6 of 9 score points across six independent confirmations:
  1. EMA 200 trend bias             (+2 / -2)
  2. EMA 21 vs EMA 55 alignment     (+2 / -2)
  3. Supertrend direction            (+2 / -2)
  4. ADX > 20 + DI direction         (+1 / -1)
  5. RSI momentum zone (not extreme) (+1 / -1)
  6. Volume ≥ 1.2× 20-bar average    (+1 / -1)

Stops: 1.5× ATR.  Target: 3× ATR (2:1 R:R).
Recommended lookback: 3+ days (EMA 200 needs warmup).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .base import LONG, SHORT, StrategyContext, StrategyMeta, base_frame, ema, finalize, sma


META = StrategyMeta(
    id="precision_momentum",
    name="Precision Momentum",
    description=(
        "6-layer confirmation: EMA 21/200 alignment, Supertrend, ADX trend strength, "
        "RSI momentum zone, and volume spike. Fires only when ≥6/9 signals agree. "
        "ATR stops, 2:1 R:R. Targets 70 %+ win rate."
    ),
    chart_label="EMA 200",
    score_label="PM Score",
)

CHART_CONFIG = {
    "overlays": ["ema", "supertrend"],
    "signals": True,
    "extra_cols": ["adx"],
}

# Tunable thresholds ─────────────────────────────────────────────────────────
_ADX_MIN      = 20      # minimum ADX to consider market trending
_RSI_BULL_LO  = 48      # RSI lower bound for long momentum zone
_RSI_BULL_HI  = 72      # RSI upper bound (avoid overbought entries)
_RSI_BEAR_LO  = 28      # RSI lower bound for short momentum zone
_RSI_BEAR_HI  = 52      # RSI upper bound for short momentum zone
_VOL_RATIO    = 1.2     # volume must exceed N× 20-bar average
_SCORE_LONG   = 6       # minimum score to emit a long signal
_SCORE_SHORT  = -6      # maximum score to emit a short signal
_ATR_STOP     = 1.5     # stop distance in ATR multiples
_ATR_TARGET   = 3.0     # target distance in ATR multiples (2:1 R:R)


def _adx(df: pd.DataFrame, n: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Wilder's ADX, +DI, -DI."""
    high, low, close = df["High"], df["Low"], df["Close"]
    up = high - high.shift(1)
    dn = low.shift(1) - low
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)

    plus_dm  = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)

    atr14    = tr.ewm(com=n - 1, adjust=False).mean()
    safe_atr = atr14.replace(0, np.nan)
    plus_di  = 100 * plus_dm.ewm(com=n - 1, adjust=False).mean()  / safe_atr
    minus_di = 100 * minus_dm.ewm(com=n - 1, adjust=False).mean() / safe_atr

    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx     = 100 * (plus_di - minus_di).abs() / di_sum
    adx    = dx.ewm(com=n - 1, adjust=False).mean()
    return plus_di, minus_di, adx


def analyze(bars: pd.DataFrame, ctx: StrategyContext) -> dict:
    frame = base_frame(bars)
    close = frame["Close"]

    # ── Trend layers ──────────────────────────────────────────────────────────
    e9   = ema(close, 9)
    e21  = ema(close, 21)
    e55  = ema(close, 55)
    e200 = ema(close, 200)

    # ── ADX ───────────────────────────────────────────────────────────────────
    plus_di, minus_di, adx_series = _adx(frame)
    is_trending = adx_series > _ADX_MIN

    # ── Volume confirmation ───────────────────────────────────────────────────
    vol      = frame["Volume"].replace(0, np.nan)
    vol_sma  = vol.rolling(20).mean()
    vol_bull = (vol / vol_sma.replace(0, np.nan) >= _VOL_RATIO) & (close > e21)
    vol_bear = (vol / vol_sma.replace(0, np.nan) >= _VOL_RATIO) & (close < e21)

    # ── RSI from base_frame ───────────────────────────────────────────────────
    rsi = frame["rsi"]

    # ── Supertrend from base_frame ────────────────────────────────────────────
    st_line = frame["st_line"]
    st_bull = close > st_line
    st_bear = close < st_line

    # ── Score (max +9 / min -9) ───────────────────────────────────────────────
    #  Component 1: major trend (EMA 200)
    c1 = np.where(close > e200, 2.0, -2.0)
    #  Component 2: intermediate trend (EMA 21 vs 55)
    c2 = np.where(e21 > e55, 2.0, -2.0)
    #  Component 3: supertrend
    c3 = np.where(st_bull, 2.0, -2.0)
    #  Component 4: ADX trend direction
    c4 = np.where(
        is_trending & (plus_di > minus_di),  1.0,
        np.where(is_trending & (minus_di > plus_di), -1.0, 0.0),
    )
    #  Component 5: RSI momentum zone
    c5 = np.where(
        (rsi >= _RSI_BULL_LO) & (rsi <= _RSI_BULL_HI),  1.0,
        np.where((rsi >= _RSI_BEAR_LO) & (rsi <= _RSI_BEAR_HI), -1.0, 0.0),
    )
    #  Component 6: volume spike in trend direction
    c6 = np.where(vol_bull, 1.0, np.where(vol_bear, -1.0, 0.0))

    score = pd.Series(c1 + c2 + c3 + c4 + c5 + c6, index=frame.index)
    frame["score"] = score.round().astype(int)
    frame["adx"]   = adx_series

    # ── Entry signals ─────────────────────────────────────────────────────────
    long_entry  = (score >= _SCORE_LONG)  & st_bull & is_trending
    short_entry = (score <= _SCORE_SHORT) & st_bear & is_trending & ctx.allow_shorts

    frame.loc[long_entry,  "entry_side"] = LONG
    frame.loc[short_entry, "entry_side"] = SHORT

    # ── ATR-based stops and targets ───────────────────────────────────────────
    atr = frame["atr"]
    frame["stop_px"] = np.nan
    frame["tp2_px"]  = np.nan
    frame.loc[long_entry,  "stop_px"] = close[long_entry]  - _ATR_STOP   * atr[long_entry]
    frame.loc[long_entry,  "tp2_px"]  = close[long_entry]  + _ATR_TARGET * atr[long_entry]
    frame.loc[short_entry, "stop_px"] = close[short_entry] + _ATR_STOP   * atr[short_entry]
    frame.loc[short_entry, "tp2_px"]  = close[short_entry] - _ATR_TARGET * atr[short_entry]

    # ── Exit signals ──────────────────────────────────────────────────────────
    # Exit long: EMA 9 loses EMA 21, supertrend flips, or RSI collapses
    frame["exit_long"]  = (e9 < e21) | (close < st_line) | (rsi < 40)
    # Exit short: EMA 9 reclaims EMA 21, supertrend flips, or RSI rebounds
    frame["exit_short"] = (e9 > e21) | (close > st_line) | (rsi > 60)

    # ── Chart lines: EMA 21 (fast), EMA 200 (slow) ───────────────────────────
    frame["ema_fast"] = e21
    frame["ema_slow"] = e200

    frame["reason"] = (
        "EMA 21/200 alignment + Supertrend + ADX(>20) + RSI zone + volume spike — "
        f"score {int(frame['score'].iloc[-1])} / 9"
    )
    return finalize(META, frame, ctx)
