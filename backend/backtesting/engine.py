from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import time as _Time
from typing import Any, Callable

import numpy as np
import pandas as pd

from strategies.base import LONG, SHORT, FLAT, StrategyContext, derive_brackets, risk_per_unit

from .config import BacktestConfig
from .metrics import compute_summary


TradeVerifier = Callable[[dict[str, Any]], dict[str, Any]]
ProgressCallback = Callable[[dict[str, Any]], None]

AI_PREFERRED_COLUMNS = [
    "Open",
    "High",
    "Low",
    "Close",
    "Volume",
    "score",
    "raw_score",
    "rsi",
    "atr",
    "ema_fast",
    "ema_slow",
    "ema_9",
    "ema_21",
    "vwap",
    "rolling_vwap",
    "supertrend",
    "st_line",
    "st_dir",
    "bb_lower",
    "bb_mid",
    "bb_upper",
    "macd",
    "macd_signal",
    "macd_hist",
    "entry_side",
    "exit_long",
    "exit_short",
    "stop_px",
    "target_px",
    "tp1_px",
    "tp2_px",
    "reason",
]


@dataclass
class OpenPosition:
    side: int = FLAT
    entry_ts: pd.Timestamp | None = None
    entry_px: float = np.nan
    raw_entry_px: float = np.nan
    stop_px: float = np.nan
    target_px: float = np.nan
    qty: float = 0.0
    entry_fee: float = 0.0
    initial_risk: float = np.nan
    initial_risk_amount: float = 0.0
    notional: float = 0.0
    leverage_used: float = 1.0
    ai_confidence: float | None = None
    ai_reason: str | None = None
    ai_model: str | None = None
    ai_risks: list[str] | None = None


def _side_label(side: int) -> str:
    if side == LONG:
        return "LONG"
    if side == SHORT:
        return "SHORT"
    return "FLAT"


def _rate(bps: float) -> float:
    return max(0.0, float(bps or 0.0)) / 10_000.0


def _effective_price(raw_px: float, side: int, action: str, cfg: BacktestConfig) -> float:
    spread_half = _rate(cfg.spread_bps) / 2
    slippage = _rate(cfg.slippage_bps)
    is_buy = (action == "entry" and side == LONG) or (action == "exit" and side == SHORT)
    adj = spread_half + slippage
    return raw_px * (1 + adj) if is_buy else raw_px * (1 - adj)


def _fee(px: float, qty: float, cfg: BacktestConfig) -> float:
    return abs(px * qty) * _rate(cfg.commission_bps)


def _as_ts(ts: pd.Timestamp | None) -> pd.Timestamp | None:
    if ts is None or pd.isna(ts):
        return None
    return ts


def _json_ts(ts: pd.Timestamp | None) -> str | None:
    ts = _as_ts(ts)
    return ts.isoformat() if ts is not None else None


def _duration_minutes(start: pd.Timestamp | None, end: pd.Timestamp | None) -> float | None:
    if start is None or end is None:
        return None
    return (end - start).total_seconds() / 60


def _to_naive(ts: pd.Timestamp | None) -> pd.Timestamp | None:
    ts = _as_ts(ts)
    if ts is None:
        return None
    if ts.tzinfo is not None:
        return ts.tz_convert(None)
    return ts


def _short_text(value: object, limit: int = 180) -> str:
    text = str(value or "").replace("\n", " ").strip()
    return text[:limit] + ("..." if len(text) > limit else "")


def _json_value(value: object) -> object:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, pd.Timestamp):
        return _json_ts(value)
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (int, str)):
        return value
    if isinstance(value, (float, np.floating)):
        value = float(value)
        return value if np.isfinite(value) else None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value)[:500]


def _compute_garch_vol_series(
    close: pd.Series, omega: float, alpha: float, beta: float
) -> pd.Series:
    """Rolling GARCH(1,1) annualised-vol estimate per bar (used for adaptive sizing)."""
    rets = close.pct_change().fillna(0.0).values.astype(np.float64)
    n = len(rets)
    vols = np.empty(n, dtype=np.float64)
    init_n = min(50, n)
    var = float(np.var(rets[1:init_n]) or 1e-10)
    for i, r in enumerate(rets):
        var = omega + alpha * r * r + beta * var
        vols[i] = math.sqrt(max(var, 1e-16))
    return pd.Series(vols, index=close.index)


def _float_or_nan(value: object) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return np.nan
    return out if np.isfinite(out) else np.nan


def _ai_columns(data: pd.DataFrame) -> list[str]:
    selected: list[str] = []
    for col in AI_PREFERRED_COLUMNS:
        if col in data.columns and col not in selected:
            selected.append(col)

    for col in data.columns:
        if col in selected or len(selected) >= 32:
            continue
        sample = data[col].dropna().tail(1)
        if sample.empty:
            continue
        value = sample.iloc[0]
        if isinstance(value, (str, int, float, bool, np.integer, np.floating, np.bool_)):
            selected.append(col)
    return selected


def _recent_ai_candles(data: pd.DataFrame, signal_ts: pd.Timestamp, limit: int) -> list[dict[str, object]]:
    try:
        window = data.loc[:signal_ts].tail(limit)
    except Exception:
        window = data.tail(limit)
    cols = _ai_columns(window)
    rows: list[dict[str, object]] = []
    for ts, row in window.iterrows():
        item: dict[str, object] = {"time": _json_ts(ts)}
        for col in cols:
            key = col.lower() if col in {"Open", "High", "Low", "Close", "Volume"} else col
            item[key] = _json_value(row.get(col))
        rows.append(item)
    return rows


def _ai_trade_payload(
    data: pd.DataFrame,
    cfg: BacktestConfig,
    ctx: StrategyContext,
    ts: pd.Timestamp,
    raw_px: float,
    side: int,
    row: pd.Series,
    equity: float,
    entry_px: float,
    stop_px: float,
    target_px: float,
    qty: float,
    risk_amount: float,
    notional: float,
    leverage_used: float,
) -> dict[str, object]:
    signal_ts = _as_ts(row.name if isinstance(row.name, pd.Timestamp) else None) or ts
    return {
        "pair": cfg.pair,
        "market": cfg.market,
        "mode": cfg.mode,
        "timeframe": cfg.timeframe,
        "strategy": {
            "id": ctx.extras.get("strategy_id", cfg.strategy),
            "title": ctx.extras.get("strategy_title", cfg.strategy),
            "description": ctx.extras.get("strategy_description"),
        },
        "data_visibility": {
            "signal_time": _json_ts(signal_ts),
            "fill_time": _json_ts(ts),
            "fill_model": cfg.fill_model,
            "known_candles_end_at": _json_ts(signal_ts),
            "note": "For next_open fills, candles stop at the signal candle to avoid lookahead.",
        },
        "candidate_position": {
            "side": _side_label(side),
            "raw_fill_price": raw_px,
            "effective_entry_price": entry_px,
            "stop_price": stop_px,
            "target_price": target_px,
            "qty": qty,
            "notional": notional,
            "equity": equity,
            "risk_amount": risk_amount,
            "risk_per_unit": risk_per_unit(entry_px, stop_px),
            "reward_risk": abs(target_px - entry_px) / max(risk_per_unit(entry_px, stop_px), 1e-9),
            "configured_risk_reward_ratio": cfg.risk_reward_ratio,
            "target_mode": cfg.target_mode,
            "leverage_used": leverage_used,
            "max_leverage": cfg.leverage,
            "commission_bps": cfg.commission_bps,
            "spread_bps": cfg.spread_bps,
            "slippage_bps": cfg.slippage_bps,
            "strategy_reason": _json_value(row.get("reason")),
        },
        "recent_candles": _recent_ai_candles(data, signal_ts, cfg.ai_candles),
    }


def _risk_amount(equity: float, cfg: BacktestConfig) -> float:
    if cfg.risk_mode == "percent_equity":
        pct = max(0.0, min(float(cfg.risk or 0.0), 100.0)) / 100.0
        return max(equity, 0.0) * pct
    return max(float(cfg.risk or 0.0), 0.0)


def _size_position(
    entry_px: float,
    stop_px: float,
    equity: float,
    cfg: BacktestConfig,
    garch_vol: float | None = None,
) -> tuple[float, float, float]:
    risk_unit = risk_per_unit(entry_px, stop_px)
    if cfg.position_sizing == "garch_adaptive":
        # Scale effective risk-per-unit by GARCH volatility → smaller size in high-vol regimes
        garch_risk = max(garch_vol * entry_px, risk_unit) if (garch_vol and garch_vol > 0) else risk_unit
        risk_amount = _risk_amount(equity, cfg)
        qty = max(risk_amount / garch_risk, 0.0)
        return qty, garch_risk, risk_amount
    if cfg.position_sizing == "cash_fraction":
        frac = max(0.001, min(cfg.risk / 100, 1.0))
        notional = equity * frac * cfg.leverage
        qty = max(notional / entry_px, 0.0)
        return qty, risk_unit, qty * risk_unit
    if cfg.position_sizing == "fixed_qty":
        qty = max(cfg.risk, 0.0)
        return qty, risk_unit, qty * risk_unit
    risk_amount = _risk_amount(equity, cfg)
    return max(risk_amount / risk_unit, 0.0), risk_unit, risk_amount


def _target_from_rr(side: int, entry_px: float, stop_px: float, cfg: BacktestConfig) -> float:
    risk_unit = risk_per_unit(entry_px, stop_px)
    rr = max(float(cfg.risk_reward_ratio or 2.0), 0.1)
    return entry_px + risk_unit * rr if side == LONG else entry_px - risk_unit * rr


def _choose_target(side: int, entry_px: float, stop_px: float, target_hint: object, cfg: BacktestConfig) -> float:
    target_hint_float = _float_or_nan(target_hint)
    target_valid = (
        np.isfinite(target_hint_float)
        and ((side == LONG and target_hint_float > entry_px) or (side == SHORT and target_hint_float < entry_px))
    )
    if cfg.target_mode == "risk_reward" or not target_valid:
        return _target_from_rr(side, entry_px, stop_px, cfg)
    return target_hint_float


def run_backtest(
    bars: pd.DataFrame,
    frame: pd.DataFrame,
    ctx: StrategyContext,
    cfg: BacktestConfig,
    verify_trade: TradeVerifier | None = None,
    progress: ProgressCallback | None = None,
) -> dict:
    cfg = cfg.normalized()
    data = bars.join(
        frame.drop(columns=[c for c in ["Open", "High", "Low", "Close", "Volume"] if c in frame.columns], errors="ignore"),
        how="left",
    )
    data = data.dropna(subset=["Open", "High", "Low", "Close"]).copy()
    if data.empty:
        return {"ok": False, "message": "No valid OHLCV bars available for backtest."}

    # Pre-compute GARCH vol series for adaptive sizing
    garch_vol_series: pd.Series | None = None
    if cfg.position_sizing == "garch_adaptive":
        garch_vol_series = _compute_garch_vol_series(
            data["Close"], cfg.garch_omega, cfg.garch_alpha, cfg.garch_beta
        )

    # Parse EOD exit time once
    _eod_time: _Time | None = None
    if cfg.eod_exit:
        try:
            _h, _m = cfg.eod_exit_time.split(":")
            _eod_time = _Time(int(_h), int(_m))
        except Exception:
            pass

    cash = float(cfg.initial_capital)
    pos = OpenPosition()
    trades: list[dict] = []
    events: list[str] = []
    equity_rows: list[dict] = []
    skip_counts: dict[str, int] = {}
    pending_entry: dict | None = None
    pending_reverse: dict | None = None
    pending_exit: str | None = None
    closed_this_bar = False
    rows = list(data.iterrows())
    total_bars = max(len(rows), 1)
    bar_cursor = 0
    ai_checks = 0
    ai_approved = 0

    def emit_progress(
        stage: str = "simulating",
        *,
        message: str | None = None,
        event: str | None = None,
    ) -> None:
        if progress is None:
            return
        try:
            progress({
                "stage": stage,
                "progress": min(max((bar_cursor + 1) / total_bars, 0.0), 1.0),
                "message": message,
                "event": event,
                "stats": {
                    "bars_done": min(bar_cursor + 1, total_bars),
                    "bars_total": total_bars,
                    "trades": len(trades),
                    "skipped": sum(skip_counts.values()),
                    "ai_checks": ai_checks,
                    "ai_approved": ai_approved,
                },
            })
        except Exception:
            pass

    def add_skip(ts: pd.Timestamp, side: int, reason: str, detail: str) -> None:
        skip_counts[reason] = skip_counts.get(reason, 0) + 1
        event = f"[{ts.strftime('%Y-%m-%d %H:%M')}] SKIP {_side_label(side)} | {detail}"
        events.append(event)
        emit_progress(message=detail, event=event)

    def mark_equity(ts: pd.Timestamp, close_px: float) -> float:
        open_value = 0.0
        unrealized = 0.0
        if pos.side != FLAT and pos.qty > 0:
            exit_px = _effective_price(close_px, pos.side, "exit", cfg)
            open_value = abs(exit_px * pos.qty)
            unrealized = (exit_px - pos.entry_px) * pos.qty if pos.side == LONG else (pos.entry_px - exit_px) * pos.qty
        equity = cash + unrealized
        equity_rows.append({"ts": ts, "equity": equity, "cash": cash, "position_value": open_value})
        return equity

    def close_position(ts: pd.Timestamp, raw_px: float, reason: str) -> None:
        nonlocal cash, pos, closed_this_bar
        if pos.side == FLAT:
            return
        exit_px = _effective_price(float(raw_px), pos.side, "exit", cfg)
        exit_fee = _fee(exit_px, pos.qty, cfg)
        gross = (exit_px - pos.entry_px) * pos.qty if pos.side == LONG else (pos.entry_px - exit_px) * pos.qty
        fees = pos.entry_fee + exit_fee
        net_cash = gross - exit_fee
        net = gross - fees
        cash += net_cash
        entry_notional = abs(pos.entry_px * pos.qty)
        ret_pct = (net / entry_notional * 100) if entry_notional else None
        r_mult = (net / pos.initial_risk_amount) if pos.initial_risk_amount > 0 else None
        trade = {
            "side": _side_label(pos.side),
            "entry_ts": _json_ts(pos.entry_ts),
            "exit_ts": _json_ts(ts),
            "entry_px": pos.entry_px,
            "raw_entry_px": pos.raw_entry_px,
            "exit_px": exit_px,
            "raw_exit_px": float(raw_px),
            "qty": pos.qty,
            "notional": pos.notional,
            "leverage_used": pos.leverage_used,
            "max_leverage": cfg.leverage,
            "risk_amount": pos.initial_risk_amount,
            "gross_pnl": gross,
            "fees": fees,
            "net_pnl": net,
            "return_pct": ret_pct,
            "r_multiple": r_mult,
            "exit_reason": reason,
            "duration_minutes": _duration_minutes(pos.entry_ts, ts),
            "stop_px": pos.stop_px,
            "target_px": pos.target_px,
            "ai_confidence": pos.ai_confidence,
            "ai_reason": pos.ai_reason,
            "ai_model": pos.ai_model,
            "ai_risks": pos.ai_risks or [],
        }
        trades.append(trade)
        event = f"[{ts.strftime('%Y-%m-%d %H:%M')}] EXIT {_side_label(pos.side)} {reason} | PnL={net:,.2f}"
        events.append(event)
        emit_progress(message=f"Closed {_side_label(pos.side)} with {reason}.", event=event)
        pos = OpenPosition()
        closed_this_bar = True

    def open_position(ts: pd.Timestamp, raw_px: float, side: int, row: pd.Series, equity: float) -> None:
        nonlocal cash, pos, ai_checks, ai_approved
        if equity <= 0:
            add_skip(ts, side, "no_equity", f"Equity={equity:,.2f}; no capital available.")
            return
        entry_px = _effective_price(float(raw_px), side, "entry", cfg)
        # Resolve GARCH vol for current bar
        garch_vol: float | None = None
        if garch_vol_series is not None:
            _gv = garch_vol_series.get(ts)
            garch_vol = float(_gv) if _gv is not None and np.isfinite(_gv) else None
        stop_hint = _float_or_nan(row.get("stop_px", np.nan))
        target_hint = _float_or_nan(row.get("tp2_px", row.get("target_px", np.nan)))
        stop_valid = (
            np.isfinite(stop_hint)
            and ((side == LONG and stop_hint < entry_px) or (side == SHORT and stop_hint > entry_px))
        )
        if np.isfinite(stop_hint) and not stop_valid:
            add_skip(
                ts,
                side,
                "invalid_stop",
                f"Fill price={entry_px:,.4f} crossed planned stop={stop_hint:,.4f}; setup invalid.",
            )
            return
        if stop_valid:
            stop_px = stop_hint
            target_px = _choose_target(side, entry_px, stop_px, target_hint, cfg)
            init_risk = risk_per_unit(entry_px, stop_px)
        else:
            stop_anchor = stop_hint if np.isfinite(stop_hint) else _float_or_nan(row.get("st_line", np.nan))
            stop_px, _target_px, init_risk = derive_brackets(side, entry_px, stop_anchor, _float_or_nan(row.get("atr", np.nan)))
            target_px = _choose_target(side, entry_px, stop_px, target_hint, cfg)
        qty, init_risk, risk_amount = _size_position(entry_px, stop_px, equity, cfg, garch_vol)
        if qty <= 0:
            add_skip(ts, side, "no_qty", "Calculated quantity is zero.")
            return
        notional = abs(entry_px * qty)
        max_notional = max(equity, 0.0) * cfg.leverage
        if notional > max_notional + 1e-9:
            required_lev = notional / max(equity, 1e-9)
            add_skip(
                ts,
                side,
                "leverage",
                f"Need notional={notional:,.2f}, equity={equity:,.2f}, "
                f"required leverage={required_lev:.2f}x > max {cfg.leverage:.2f}x",
            )
            return
        leverage_used = max(1.0, notional / max(equity, 1e-9))
        entry_fee = _fee(entry_px, qty, cfg)
        if entry_fee >= cash:
            add_skip(ts, side, "fee", f"Entry fee={entry_fee:,.2f} exceeds cash={cash:,.2f}")
            return
        ai_decision: dict[str, Any] | None = None
        if cfg.ai_verification_enabled:
            if verify_trade is None:
                add_skip(ts, side, "ai_error", "AI verification is enabled but no verifier is configured.")
                return
            payload = _ai_trade_payload(
                data=data,
                cfg=cfg,
                ctx=ctx,
                ts=ts,
                raw_px=float(raw_px),
                side=side,
                row=row,
                equity=equity,
                entry_px=entry_px,
                stop_px=stop_px,
                target_px=target_px,
                qty=qty,
                risk_amount=risk_amount,
                notional=notional,
                leverage_used=leverage_used,
            )
            candle_count = len(payload.get("recent_candles") or [])
            if candle_count < min(20, cfg.ai_candles):
                add_skip(ts, side, "ai_context", f"AI verification needs more candles; only {candle_count} available.")
                return
            ai_checks += 1
            emit_progress(
                "ai_verification",
                message=f"Checking {_side_label(side)} with AI ({ai_checks} checks).",
                event=f"[{ts.strftime('%Y-%m-%d %H:%M')}] AI CHECK {_side_label(side)} | candles={candle_count}",
            )
            try:
                ai_decision = verify_trade(payload)
            except Exception as exc:
                add_skip(ts, side, "ai_error", f"AI verification failed: {_short_text(exc)}")
                return
            confidence = ai_decision.get("confidence") if isinstance(ai_decision, dict) else None
            if not isinstance(ai_decision, dict) or not ai_decision.get("ok"):
                reason = ai_decision.get("reason") if isinstance(ai_decision, dict) else "No AI decision returned."
                add_skip(ts, side, "ai_error", _short_text(reason))
                return
            if not ai_decision.get("approved"):
                try:
                    conf_value = float(confidence)
                except (TypeError, ValueError):
                    conf_value = 0.0
                reason_key = "ai_low_confidence" if conf_value < cfg.ai_min_confidence else "ai_rejected"
                add_skip(
                    ts,
                    side,
                    reason_key,
                    f"AI confidence={conf_value:.1f} min={cfg.ai_min_confidence:.1f}; "
                    f"{_short_text(ai_decision.get('reason'))}",
                )
                return
            ai_approved += 1
            event = (
                f"[{ts.strftime('%Y-%m-%d %H:%M')}] AI APPROVED {_side_label(side)} | "
                f"confidence={float(confidence or 0):.1f} min={cfg.ai_min_confidence:.1f} "
                f"model={_short_text(ai_decision.get('model'), 40)}"
            )
            events.append(event)
            emit_progress("ai_verification", message="AI approved candidate entry.", event=event)
        cash -= entry_fee
        pos = OpenPosition(
            side=side,
            entry_ts=ts,
            entry_px=entry_px,
            raw_entry_px=float(raw_px),
            stop_px=stop_px,
            target_px=target_px,
            qty=qty,
            entry_fee=entry_fee,
            initial_risk=init_risk,
            initial_risk_amount=risk_amount,
            notional=notional,
            leverage_used=leverage_used,
            ai_confidence=float(ai_decision["confidence"]) if ai_decision and ai_decision.get("confidence") is not None else None,
            ai_reason=str(ai_decision.get("reason")) if ai_decision else None,
            ai_model=str(ai_decision.get("model")) if ai_decision else None,
            ai_risks=list(ai_decision.get("risks") or []) if ai_decision else None,
        )
        event = (
            f"[{ts.strftime('%Y-%m-%d %H:%M')}] ENTRY {_side_label(side)} | "
            f"Entry={entry_px:,.4f} Qty={qty:,.8f} Notional={notional:,.2f} "
            f"Lev={leverage_used:.2f}x/{cfg.leverage:.2f}x"
        )
        events.append(event)
        emit_progress(message=f"Opened {_side_label(side)} position.", event=event)

    def update_trailing_stop(ts: pd.Timestamp, row: pd.Series) -> None:
        close_px = float(row.get("Close", np.nan))
        if pos.side == LONG and np.isfinite(row.get("st_line", np.nan)):
            next_stop = max(pos.stop_px, float(row.get("st_line")))
            if np.isfinite(close_px) and next_stop < close_px and next_stop > pos.stop_px + 1e-9:
                pos.stop_px = next_stop
                event = f"[{ts.strftime('%Y-%m-%d %H:%M')}] TRAIL LONG SL -> {pos.stop_px:,.4f}"
                events.append(event)
                emit_progress(message="Updated trailing stop.", event=event)
        elif pos.side == SHORT and np.isfinite(row.get("st_line", np.nan)):
            next_stop = min(pos.stop_px, float(row.get("st_line")))
            if np.isfinite(close_px) and next_stop > close_px and next_stop < pos.stop_px - 1e-9:
                pos.stop_px = next_stop
                event = f"[{ts.strftime('%Y-%m-%d %H:%M')}] TRAIL SHORT SL -> {pos.stop_px:,.4f}"
                events.append(event)
                emit_progress(message="Updated trailing stop.", event=event)

    def maybe_exit_for_stop_target(ts: pd.Timestamp, raw_open: float, raw_high: float, raw_low: float) -> bool:
        if pos.side == LONG:
            stop_hit = raw_low <= pos.stop_px
            target_hit = raw_high >= pos.target_px
            if stop_hit and raw_open <= pos.stop_px:
                close_position(ts, raw_open, "STOP_GAP")
                return True
            if target_hit and raw_open >= pos.target_px:
                close_position(ts, raw_open, "TARGET_GAP")
                return True
            if stop_hit and target_hit:
                if cfg.same_bar_priority == "target_first":
                    close_position(ts, pos.target_px, "TARGET_SAME_BAR")
                else:
                    close_position(ts, pos.stop_px, "STOP_SAME_BAR")
                return True
            if stop_hit:
                close_position(ts, pos.stop_px, "STOP")
                return True
            if target_hit:
                close_position(ts, pos.target_px, "TARGET")
                return True
        elif pos.side == SHORT:
            stop_hit = raw_high >= pos.stop_px
            target_hit = raw_low <= pos.target_px
            if stop_hit and raw_open >= pos.stop_px:
                close_position(ts, raw_open, "STOP_GAP")
                return True
            if target_hit and raw_open <= pos.target_px:
                close_position(ts, raw_open, "TARGET_GAP")
                return True
            if stop_hit and target_hit:
                if cfg.same_bar_priority == "target_first":
                    close_position(ts, pos.target_px, "TARGET_SAME_BAR")
                else:
                    close_position(ts, pos.stop_px, "STOP_SAME_BAR")
                return True
            if stop_hit:
                close_position(ts, pos.stop_px, "STOP")
                return True
            if target_hit:
                close_position(ts, pos.target_px, "TARGET")
                return True
        return False

    for i, (ts, row) in enumerate(rows):
        bar_cursor = i
        closed_this_bar = False
        raw_open = float(row["Open"])
        raw_high = float(row["High"])
        raw_low = float(row["Low"])
        raw_close = float(row["Close"])
        current_equity = mark_equity(ts, raw_close)

        if cfg.fill_model == "next_open":
            if pending_exit and pos.side != FLAT:
                close_position(ts, raw_open, pending_exit)
                pending_exit = None
                current_equity = mark_equity(ts, raw_close)
            if pending_reverse and pos.side == FLAT:
                reverse_side = int(pending_reverse["side"])
                if reverse_side == SHORT and not cfg.allow_shorts:
                    add_skip(ts, reverse_side, "shorts_disabled", f"SHORT ignored in {cfg.mode.upper()} mode.")
                else:
                    # Re-compute equity after close so position sizing uses fresh capital
                    rev_equity = mark_equity(ts, raw_close)
                    open_position(ts, raw_open, reverse_side, pending_reverse["row"], rev_equity)
                pending_reverse = None
                pending_entry = None
            if pending_entry and pos.side == FLAT and not closed_this_bar:
                entry_side = int(pending_entry["side"])
                pending_row = pending_entry["row"]
                pending_score = abs(float(pending_row.get("score", 0) or 0))
                if cfg.min_signal_score > 0 and pending_score < cfg.min_signal_score:
                    add_skip(ts, entry_side, "weak_signal",
                             f"score={pending_score:.2f} < min={cfg.min_signal_score:.2f}")
                elif entry_side == SHORT and not cfg.allow_shorts:
                    add_skip(ts, entry_side, "shorts_disabled", f"SHORT ignored in {cfg.mode.upper()} mode.")
                else:
                    open_position(ts, raw_open, entry_side, pending_row, current_equity)
                pending_entry = None

        if pos.side != FLAT:
            exited_intrabar = maybe_exit_for_stop_target(ts, raw_open, raw_high, raw_low)
            if not exited_intrabar and pos.side != FLAT:
                signal_side = int(row.get("entry_side", 0) or 0)
                opposite_side = SHORT if pos.side == LONG else LONG
                has_opposite_signal = signal_side == opposite_side
                if has_opposite_signal and cfg.opposite_signal_mode in {"exit_only", "reverse"}:
                    if cfg.fill_model == "close":
                        close_position(ts, raw_close, "OPPOSITE")
                        if cfg.opposite_signal_mode == "reverse" and not (opposite_side == SHORT and not cfg.allow_shorts):
                            open_position(ts, raw_close, opposite_side, row, cash)
                        elif cfg.opposite_signal_mode == "reverse" and opposite_side == SHORT and not cfg.allow_shorts:
                            add_skip(ts, opposite_side, "shorts_disabled", f"SHORT ignored in {cfg.mode.upper()} mode.")
                    else:
                        pending_exit = "OPPOSITE"
                        if cfg.opposite_signal_mode == "reverse":
                            pending_reverse = {"side": opposite_side, "row": row.copy()}
                    mark_equity(ts, raw_close)
                    continue

                if pos.side == LONG and bool(row.get("exit_long", False)):
                    if cfg.fill_model == "close":
                        close_position(ts, raw_close, "SIGNAL")
                    else:
                        pending_exit = "SIGNAL"
                elif pos.side == SHORT and bool(row.get("exit_short", False)):
                    if cfg.fill_model == "close":
                        close_position(ts, raw_close, "SIGNAL")
                    else:
                        pending_exit = "SIGNAL"

            if pos.side != FLAT:
                update_trailing_stop(ts, row)

        # EOD forced square-off
        if _eod_time is not None and pos.side != FLAT and not closed_this_bar:
            bar_time = ts.time() if hasattr(ts, "time") else None
            if bar_time is not None and bar_time >= _eod_time:
                if cfg.fill_model == "close":
                    close_position(ts, raw_close, "EOD")
                else:
                    pending_exit = "EOD"

        if pos.side == FLAT and not pending_exit and not closed_this_bar:
            if i < cfg.warmup_bars:
                mark_equity(ts, raw_close)
                continue
            side = int(row.get("entry_side", 0) or 0)
            if side in (LONG, SHORT):
                score_val = abs(float(row.get("score", 0) or 0))
                if cfg.min_signal_score > 0 and score_val < cfg.min_signal_score:
                    add_skip(ts, side, "weak_signal",
                             f"score={score_val:.2f} < min={cfg.min_signal_score:.2f}")
                elif cfg.fill_model == "close":
                    if side == SHORT and not cfg.allow_shorts:
                        add_skip(ts, side, "shorts_disabled", f"SHORT ignored in {cfg.mode.upper()} mode.")
                    else:
                        open_position(ts, raw_close, side, row, current_equity)
                elif i < len(rows) - 1:
                    pending_entry = {"side": side, "row": row.copy()}
                else:
                    add_skip(ts, side, "no_next_bar", "No next candle available for next-open fill.")

        mark_equity(ts, raw_close)
        if i == 0 or i == len(rows) - 1 or i % max(1, len(rows) // 100) == 0:
            emit_progress(message=f"Processed {i + 1}/{len(rows)} bars.")

    if cfg.finalize_open_trade and pos.side != FLAT:
        last_ts, last_row = rows[-1]
        close_position(last_ts, float(last_row["Close"]), "FINAL")

    equity = pd.DataFrame(equity_rows).drop_duplicates(subset=["ts"], keep="last").set_index("ts").sort_index()
    if not equity.empty:
        peak = equity["equity"].cummax().replace(0, np.nan)
        equity["drawdown_pct"] = ((equity["equity"] - peak) / peak * 100).fillna(0.0)

    median_delta = data.index.to_series().diff().median()
    bar_minutes = median_delta.total_seconds() / 60 if isinstance(median_delta, pd.Timedelta) and median_delta.total_seconds() > 0 else 15
    bars_per_year = (365.25 * 24 * 60) / bar_minutes
    summary = compute_summary(equity, trades, cfg.initial_capital, bars_per_year, risk_free_rate=cfg.risk_free_rate)
    summary["skipped_signals"] = sum(skip_counts.values())
    summary["skip_counts"] = dict(sorted(skip_counts.items()))
    summary["max_leverage"] = cfg.leverage
    summary["max_leverage_used"] = max([float(t.get("leverage_used") or 0.0) for t in trades], default=0.0)
    summary["avg_leverage_used"] = (
        sum(float(t.get("leverage_used") or 0.0) for t in trades) / len(trades) if trades else 0.0
    )
    summary["risk_reward_ratio"] = cfg.risk_reward_ratio
    summary["target_mode"] = cfg.target_mode
    summary["ai_verified_trades"] = sum(1 for t in trades if t.get("ai_confidence") is not None)
    assumption_warnings: list[str] = []
    if cfg.fill_model == "close":
        assumption_warnings.append(
            "fill_model=close can be optimistic when signals use the same candle close; prefer next_open for validation."
        )
    if len(trades) < 30:
        assumption_warnings.append("Fewer than 30 closed trades; results are not statistically strong yet.")
    if cfg.lookback_days < 90:
        assumption_warnings.append("Lookback is under 90 days; run longer windows and walk-forward tests before trusting it.")
    if cfg.commission_bps == 0 and cfg.spread_bps == 0 and cfg.slippage_bps == 0:
        assumption_warnings.append("Costs are all zero; add realistic commission, spread, and slippage before going live.")
    if summary.get("exposure_pct") is not None and float(summary.get("exposure_pct") or 0.0) < 10.0:
        assumption_warnings.append("Exposure is below 10%; active-period metrics may not reflect full portfolio performance.")
    if cfg.strategy == "daily_sweep":
        assumption_warnings.append(
            "Daily Sweep uses confirmed pivot levels only after right-side bars close; expect fewer but more realistic signals."
        )
    summary["assumption_warnings"] = assumption_warnings

    return {
        "ok": True,
        "summary": summary,
        "trades": trades,
        "equity": [
            {
                "time": _json_ts(ts),
                "equity": float(r["equity"]),
                "cash": float(r["cash"]),
                "position_value": float(r["position_value"]),
                "drawdown_pct": float(r["drawdown_pct"]),
            }
            for ts, r in equity.iterrows()
        ],
        "events": events,
        "final_state": {
            "side": _side_label(pos.side),
            "entry_ts": _json_ts(pos.entry_ts),
            "entry_px": pos.entry_px if np.isfinite(pos.entry_px) else None,
            "stop_px": pos.stop_px if np.isfinite(pos.stop_px) else None,
            "target_px": pos.target_px if np.isfinite(pos.target_px) else None,
            "qty": pos.qty,
            "notional": pos.notional,
            "leverage_used": pos.leverage_used,
            "max_leverage": cfg.leverage,
            "ai_confidence": pos.ai_confidence,
            "ai_reason": pos.ai_reason,
            "ai_model": pos.ai_model,
        },
    }


def naive_trade_row(trade: dict) -> dict:
    out = dict(trade)
    out["entry_ts"] = _to_naive(pd.Timestamp(out["entry_ts"])) if out.get("entry_ts") else None
    out["exit_ts"] = _to_naive(pd.Timestamp(out["exit_ts"])) if out.get("exit_ts") else None
    return out


def naive_equity_ts(value: str) -> pd.Timestamp:
    return _to_naive(pd.Timestamp(value)) or pd.Timestamp.utcnow().tz_localize(None)
