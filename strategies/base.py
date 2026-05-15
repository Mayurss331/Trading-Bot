from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

try:
    from numba import njit as _njit
    _NUMBA_AVAILABLE = True
except ImportError:
    _NUMBA_AVAILABLE = False
    def _njit(fn):  # no-op fallback
        return fn


LONG = 1
SHORT = -1
FLAT = 0


@dataclass(frozen=True)
class StrategyContext:
    pair: str
    market: str
    mode: str
    risk: float
    allow_shorts: bool
    extras: dict[str, Any]


@dataclass(frozen=True)
class StrategyMeta:
    id: str
    name: str
    description: str
    chart_label: str
    score_label: str


@dataclass
class PaperState:
    side: int = FLAT
    trade_id: int = 0
    entry_ts: pd.Timestamp | None = None
    entry_px: float = np.nan
    stop_px: float = np.nan
    target_px: float = np.nan
    qty: float = np.nan
    realized_pnl: float = 0.0
    qty_open: float = np.nan
    tp1_px: float = np.nan
    tp2_px: float = np.nan
    tp1_frac: float = np.nan


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def rsi_s(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    g = d.clip(lower=0).ewm(com=n - 1, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(com=n - 1, adjust=False).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    return rsi_s(s, n)


def atr_s(df: pd.DataFrame, n: int = 14) -> pd.Series:
    tr = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - df["Close"].shift(1)).abs(),
            (df["Low"] - df["Close"].shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(span=n, adjust=False).mean()


def atr(
    high_or_df: pd.DataFrame | pd.Series,
    low: pd.Series | None = None,
    close: pd.Series | None = None,
    n: int = 14,
) -> pd.Series:
    if isinstance(high_or_df, pd.DataFrame):
        return atr_s(high_or_df, n)
    if low is None or close is None:
        raise TypeError("atr expects either atr(df, n=14) or atr(high, low, close, n=14).")
    tr = pd.concat(
        [
            high_or_df - low,
            (high_or_df - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(span=n, adjust=False).mean()


def vwap(df: pd.DataFrame) -> pd.Series:
    typical = (df["High"] + df["Low"] + df["Close"]) / 3
    volume = df["Volume"].replace(0, np.nan)
    return (typical * volume).cumsum() / volume.cumsum()


def rolling_vwap(df: pd.DataFrame, n: int = 20) -> pd.Series:
    typical = (df["High"] + df["Low"] + df["Close"]) / 3
    volume = df["Volume"].replace(0, np.nan)
    return (typical * volume).rolling(n).sum() / volume.rolling(n).sum()


def crossed_above(a: pd.Series, b: pd.Series | float) -> pd.Series:
    other = b if isinstance(b, pd.Series) else pd.Series(b, index=a.index)
    return (a > other) & (a.shift(1) <= other.shift(1))


def crossed_below(a: pd.Series, b: pd.Series | float) -> pd.Series:
    other = b if isinstance(b, pd.Series) else pd.Series(b, index=a.index)
    return (a < other) & (a.shift(1) >= other.shift(1))


def highest(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).max()


def lowest(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).min()


def zscore(s: pd.Series, n: int = 20) -> pd.Series:
    mean = s.rolling(n).mean()
    std = s.rolling(n).std().replace(0, np.nan)
    return (s - mean) / std


@_njit
def _supertrend_core(close: np.ndarray, bu_b: np.ndarray, bl_b: np.ndarray) -> np.ndarray:
    """GARCH-style state loop — JIT-compiled via Numba when available."""
    n = len(close)
    bu = bu_b.copy()
    bl = bl_b.copy()
    st = np.full(n, np.nan)
    for i in range(1, n):
        bu[i] = bu_b[i] if bu_b[i] < bu[i - 1] or close[i - 1] > bu[i - 1] else bu[i - 1]
        bl[i] = bl_b[i] if bl_b[i] > bl[i - 1] or close[i - 1] < bl[i - 1] else bl[i - 1]
        prev = st[i - 1]
        if math.isnan(prev):
            st[i] = bl[i]
        elif prev == bu[i - 1]:
            st[i] = bl[i] if close[i] > bu[i] else bu[i]
        else:
            st[i] = bu[i] if close[i] < bl[i] else bl[i]
    return st


def supertrend(df: pd.DataFrame, n: int = 10, mult: float = 3.5) -> tuple[pd.Series, pd.Series]:
    mid = (df["High"] + df["Low"]) / 2
    _atr = atr_s(df, n)
    bu_b = (mid + mult * _atr).values.astype(np.float64)
    bl_b = (mid - mult * _atr).values.astype(np.float64)
    close = df["Close"].values.astype(np.float64)
    st_arr = _supertrend_core(close, bu_b, bl_b)
    st = pd.Series(st_arr, index=df.index)
    return (df["Close"] > st).astype(int), st


def bollinger(df: pd.DataFrame, n: int = 20, k: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = sma(df["Close"], n)
    std = df["Close"].rolling(n).std()
    return mid - k * std, mid, mid + k * std


def macd_hist(close: pd.Series) -> pd.Series:
    line = ema(close, 12) - ema(close, 26)
    signal = ema(line, 9)
    return line - signal


def macd(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    line = ema(close, 12) - ema(close, 26)
    signal = ema(line, 9)
    return line, signal, line - signal


def typical_price(df: pd.DataFrame) -> pd.Series:
    return (df["High"] + df["Low"] + df["Close"]) / 3


def base_frame(bars: pd.DataFrame) -> pd.DataFrame:
    frame = bars.copy()
    frame["rsi"] = rsi_s(frame["Close"])
    frame["atr"] = atr_s(frame)
    _st_dir, frame["st_line"] = supertrend(frame)
    frame["st_dir"] = _st_dir.map({1: 1, 0: -1})
    frame["entry_side"] = 0
    frame["exit_long"] = False
    frame["exit_short"] = False
    frame["score"] = 0
    frame["reason"] = ""
    return frame


def fmt_ts(ts: pd.Timestamp) -> str:
    return ts.strftime("%Y-%m-%d %H:%M")


def fmt_side(side: int) -> str:
    if side == LONG:
        return "LONG"
    if side == SHORT:
        return "SHORT"
    return "FLAT"


def risk_per_unit(entry_px: float, stop_px: float) -> float:
    risk = abs(entry_px - stop_px)
    if not np.isfinite(risk) or risk < 1e-9:
        risk = max(abs(entry_px) * 0.001, 1e-6)
    return risk


def derive_brackets(side: int, entry_px: float, stop_anchor: float, atr: float | None = None) -> tuple[float, float, float]:
    min_gap = max(abs(entry_px) * 0.001, atr or 0.0, 1e-6)
    if not np.isfinite(stop_anchor):
        stop_anchor = entry_px - min_gap if side == LONG else entry_px + min_gap
    if side == LONG:
        stop_px = stop_anchor if stop_anchor < entry_px else entry_px - max(stop_anchor - entry_px, min_gap)
        risk = risk_per_unit(entry_px, stop_px)
        return stop_px, entry_px + 2 * risk, risk
    stop_px = stop_anchor if stop_anchor > entry_px else entry_px + max(entry_px - stop_anchor, min_gap)
    risk = risk_per_unit(entry_px, stop_px)
    return stop_px, entry_px - 2 * risk, risk


def state_payload(state: PaperState) -> dict[str, Any]:
    return {
        "side": fmt_side(state.side),
        "trade_id": state.trade_id,
        "entry_ts": state.entry_ts,
        "entry_px": state.entry_px,
        "stop_px": state.stop_px,
        "target_px": state.target_px,
        "qty": state.qty,
        "qty_open": state.qty_open,
        "tp1_px": state.tp1_px,
        "tp2_px": state.tp2_px,
        "tp1_frac": state.tp1_frac,
        "realized_pnl": state.realized_pnl,
        "broker_order_status": None,
        "exit_pending": False,
        "position_id": None,
    }


def enter_trade(state: PaperState, side: int, ts: pd.Timestamp, row: pd.Series, ctx: StrategyContext) -> str:
    entry = float(row["Close"])
    stop_hint = row.get("stop_px", np.nan)
    tp1_hint = row.get("tp1_px", np.nan)
    tp2_hint = row.get("tp2_px", np.nan)
    tp1_frac = row.get("tp1_frac", np.nan)
    use_custom = np.isfinite(stop_hint) and (np.isfinite(tp1_hint) or np.isfinite(tp2_hint))
    if use_custom:
        stop = float(stop_hint)
        if np.isfinite(tp1_hint) and np.isfinite(tp2_hint):
            target = float(tp2_hint)
        elif np.isfinite(tp2_hint):
            target = float(tp2_hint)
        else:
            target = float(tp1_hint)
        risk = risk_per_unit(entry, stop)
    else:
        stop, target, risk = derive_brackets(side, entry, float(row.get("st_line", np.nan)), float(row.get("atr", np.nan)))
    qty = ctx.risk / risk if risk > 0 else 0.0
    state.side = side
    state.trade_id += 1
    state.entry_ts = ts
    state.entry_px = entry
    state.stop_px = stop
    state.target_px = target
    state.qty = qty
    state.qty_open = qty
    state.tp1_px = float(tp1_hint) if np.isfinite(tp1_hint) else np.nan
    state.tp2_px = float(tp2_hint) if np.isfinite(tp2_hint) else np.nan
    state.tp1_frac = float(tp1_frac) if np.isfinite(tp1_frac) else np.nan
    notional = qty * entry
    return (
        f"[{fmt_ts(ts)}] ENTRY {fmt_side(side)} | Trade #{state.trade_id} | "
        f"Entry={entry:,.4f} SL={stop:,.4f} Target={target:,.4f} | "
        f"Qty={qty:,.8f} (Risk ${ctx.risk:.2f}, Notional ${notional:,.2f})"
    )


def exit_trade(state: PaperState, ts: pd.Timestamp, exit_px: float, reason: str, ctx: StrategyContext) -> str:
    qty_exit = state.qty_open if np.isfinite(state.qty_open) else state.qty
    if state.side == LONG:
        pnl = (exit_px - state.entry_px) * qty_exit
    else:
        pnl = (state.entry_px - exit_px) * qty_exit
    state.realized_pnl += pnl
    r_mult = pnl / ctx.risk if ctx.risk > 0 else np.nan
    msg = (
        f"[{fmt_ts(ts)}] EXIT {fmt_side(state.side)} | Trade #{state.trade_id} | "
        f"Exit={exit_px:,.4f} Reason={reason} | PnL=${pnl:,.2f} ({r_mult:+.2f}R) | "
        f"Cumulative=${state.realized_pnl:,.2f}"
    )
    realized = state.realized_pnl
    trade_id = state.trade_id
    state.__dict__.update(PaperState(realized_pnl=realized, trade_id=trade_id).__dict__)
    return msg


def replay_strategy(
    frame: pd.DataFrame,
    ctx: StrategyContext,
    max_rows: int = 300,
    same_bar_priority: str = "stop_first",
) -> tuple[PaperState, list[str]]:
    state = PaperState()
    events: list[str] = []
    replay = frame.dropna(subset=["Close"]).tail(max_rows)

    for ts, row in replay.iterrows():
        close = float(row["Close"])
        high = float(row["High"])
        low = float(row["Low"])
        st_line = float(row.get("st_line", np.nan))

        if state.side == FLAT:
            side = int(row.get("entry_side", 0) or 0)
            if side == SHORT and not ctx.allow_shorts:
                events.append(f"[{fmt_ts(ts)}] SHORT SIGNAL ignored in {ctx.mode.upper()} mode.")
                continue
            if side in (LONG, SHORT):
                events.append(enter_trade(state, side, ts, row, ctx))
            continue

        if state.side == LONG:
            if np.isfinite(st_line):
                next_stop = max(state.stop_px, st_line)
                if next_stop > state.stop_px + 1e-9:
                    state.stop_px = next_stop
                    events.append(f"[{fmt_ts(ts)}] TRAIL LONG SL -> {state.stop_px:,.4f}")
            stop_hit = low <= state.stop_px
            target_hit = high >= state.target_px
            if stop_hit and target_hit:
                # Both hit same bar — use configured priority
                if same_bar_priority == "target_first":
                    events.append(exit_trade(state, ts, state.target_px, "TARGET_SAME_BAR", ctx))
                else:
                    events.append(exit_trade(state, ts, state.stop_px, "STOP_SAME_BAR", ctx))
            elif stop_hit:
                events.append(exit_trade(state, ts, state.stop_px, "STOP", ctx))
            elif np.isfinite(state.tp1_px) and np.isfinite(state.tp1_frac) and state.tp1_frac > 0 and high >= state.tp1_px:
                qty_exit = (state.qty_open if np.isfinite(state.qty_open) else state.qty) * float(state.tp1_frac)
                qty_exit = min(qty_exit, state.qty_open if np.isfinite(state.qty_open) else state.qty)
                if qty_exit > 0:
                    prev_open = state.qty_open if np.isfinite(state.qty_open) else state.qty
                    state.qty_open = max((state.qty_open if np.isfinite(state.qty_open) else state.qty) - qty_exit, 0.0)
                    pnl = (state.tp1_px - state.entry_px) * qty_exit
                    state.realized_pnl += pnl
                    r_mult = pnl / ctx.risk if ctx.risk > 0 else np.nan
                    events.append(
                        f"[{fmt_ts(ts)}] EXIT LONG (TP1) | Trade #{state.trade_id} | "
                        f"Exit={state.tp1_px:,.4f} Qty={qty_exit:,.8f}/{prev_open:,.8f} | "
                        f"PnL=${pnl:,.2f} ({r_mult:+.2f}R) | Cumulative=${state.realized_pnl:,.2f}"
                    )
                    state.tp1_px = np.nan
            elif target_hit:
                events.append(exit_trade(state, ts, state.target_px, "TARGET", ctx))
            elif bool(row.get("exit_long", False)):
                events.append(exit_trade(state, ts, close, "SIGNAL", ctx))

        elif state.side == SHORT:
            if np.isfinite(st_line):
                next_stop = min(state.stop_px, st_line)
                if next_stop < state.stop_px - 1e-9:
                    state.stop_px = next_stop
                    events.append(f"[{fmt_ts(ts)}] TRAIL SHORT SL -> {state.stop_px:,.4f}")
            stop_hit = high >= state.stop_px
            target_hit = low <= state.target_px
            if stop_hit and target_hit:
                if same_bar_priority == "target_first":
                    events.append(exit_trade(state, ts, state.target_px, "TARGET_SAME_BAR", ctx))
                else:
                    events.append(exit_trade(state, ts, state.stop_px, "STOP_SAME_BAR", ctx))
            elif stop_hit:
                events.append(exit_trade(state, ts, state.stop_px, "STOP", ctx))
            elif np.isfinite(state.tp1_px) and np.isfinite(state.tp1_frac) and state.tp1_frac > 0 and low <= state.tp1_px:
                qty_exit = (state.qty_open if np.isfinite(state.qty_open) else state.qty) * float(state.tp1_frac)
                qty_exit = min(qty_exit, state.qty_open if np.isfinite(state.qty_open) else state.qty)
                if qty_exit > 0:
                    prev_open = state.qty_open if np.isfinite(state.qty_open) else state.qty
                    state.qty_open = max((state.qty_open if np.isfinite(state.qty_open) else state.qty) - qty_exit, 0.0)
                    pnl = (state.entry_px - state.tp1_px) * qty_exit
                    state.realized_pnl += pnl
                    r_mult = pnl / ctx.risk if ctx.risk > 0 else np.nan
                    events.append(
                        f"[{fmt_ts(ts)}] EXIT SHORT (TP1) | Trade #{state.trade_id} | "
                        f"Exit={state.tp1_px:,.4f} Qty={qty_exit:,.8f}/{prev_open:,.8f} | "
                        f"PnL=${pnl:,.2f} ({r_mult:+.2f}R) | Cumulative=${state.realized_pnl:,.2f}"
                    )
                    state.tp1_px = np.nan
            elif target_hit:
                events.append(exit_trade(state, ts, state.target_px, "TARGET", ctx))
            elif bool(row.get("exit_short", False)):
                events.append(exit_trade(state, ts, close, "SIGNAL", ctx))

    return state, events[-80:]


def action_from_latest(frame: pd.DataFrame, state: PaperState, ctx: StrategyContext) -> dict[str, Any]:
    if frame.empty:
        return {"type": "WAIT", "side": "FLAT", "label": "Waiting for warmup"}
    row = frame.iloc[-1]
    score = int(row.get("score", 0) or 0)
    if state.side == FLAT:
        side = int(row.get("entry_side", 0) or 0)
        if side == LONG:
            return {"type": "ENTRY", "side": "LONG", "label": "Long entry setup"}
        if side == SHORT:
            if ctx.allow_shorts:
                return {"type": "ENTRY", "side": "SHORT", "label": "Short entry setup"}
            return {"type": "IGNORE", "side": "SHORT", "label": "Short signal ignored"}
        return {"type": "WAIT", "side": "FLAT", "label": "No entry setup"}
    if state.side == LONG and bool(row.get("exit_long", False)):
        return {"type": "EXIT", "side": "LONG", "label": "Long exit setup"}
    if state.side == SHORT and bool(row.get("exit_short", False)):
        return {"type": "EXIT", "side": "SHORT", "label": "Short exit setup"}
    return {"type": "HOLD", "side": fmt_side(state.side), "label": "Position still valid", "score": score}


def finalize(meta: StrategyMeta, frame: pd.DataFrame, ctx: StrategyContext, notes: list[str] | None = None, extra_indicators: dict | None = None) -> dict[str, Any]:
    frame = frame.dropna(subset=["Close"]).copy()
    state, events = replay_strategy(frame, ctx)
    action = action_from_latest(frame, state, ctx)
    latest = frame.iloc[-1] if not frame.empty else pd.Series(dtype=object)
    reason = str(latest.get("reason") or meta.description)
    return {
        "meta": meta,
        "frame": frame,
        "state": state_payload(state),
        "events": events,
        "action": action,
        "reason": reason,
        "indicators": {
            "score": latest.get("score"),
            "rsi": latest.get("rsi"),
            "supertrend": latest.get("st_line"),
            "ema_fast": latest.get("ema_fast"),
            "ema_slow": latest.get("ema_slow"),
            "bb_lower": latest.get("bb_lower"),
            "bb_mid": latest.get("bb_mid"),
            "bb_upper": latest.get("bb_upper"),
            "tp1_px": latest.get("tp1_px"),
            "tp2_px": latest.get("tp2_px"),
            "stop_px": latest.get("stop_px"),
            **(extra_indicators or {}),
        },
        "notes": notes or [],
    }
