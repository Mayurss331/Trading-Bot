from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from strategies.base import LONG, SHORT, FLAT, StrategyContext, derive_brackets, risk_per_unit

from .config import BacktestConfig
from .metrics import compute_summary


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


def _risk_amount(equity: float, cfg: BacktestConfig) -> float:
    if cfg.risk_mode == "percent_equity":
        pct = max(0.0, min(float(cfg.risk or 0.0), 100.0)) / 100.0
        return max(equity, 0.0) * pct
    return max(float(cfg.risk or 0.0), 0.0)


def _size_position(entry_px: float, stop_px: float, equity: float, cfg: BacktestConfig) -> tuple[float, float, float]:
    risk_unit = risk_per_unit(entry_px, stop_px)
    if cfg.position_sizing == "cash_fraction":
        notional = min(equity * cfg.leverage, equity * max(0.001, min(cfg.risk / 100, 1.0)) * cfg.leverage)
        qty = max(notional / entry_px, 0.0)
        return qty, risk_unit, qty * risk_unit
    if cfg.position_sizing == "fixed_qty":
        qty = max(cfg.risk, 0.0)
        return qty, risk_unit, qty * risk_unit
    risk_amount = _risk_amount(equity, cfg)
    return max(risk_amount / risk_unit, 0.0), risk_unit, risk_amount


def run_backtest(bars: pd.DataFrame, frame: pd.DataFrame, ctx: StrategyContext, cfg: BacktestConfig) -> dict:
    cfg = cfg.normalized()
    data = bars.join(
        frame.drop(columns=[c for c in ["Open", "High", "Low", "Close", "Volume"] if c in frame.columns], errors="ignore"),
        how="left",
    )
    data = data.dropna(subset=["Open", "High", "Low", "Close"]).copy()
    if data.empty:
        return {"ok": False, "message": "No valid OHLCV bars available for backtest."}

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

    def add_skip(ts: pd.Timestamp, side: int, reason: str, detail: str) -> None:
        skip_counts[reason] = skip_counts.get(reason, 0) + 1
        events.append(f"[{ts.strftime('%Y-%m-%d %H:%M')}] SKIP {_side_label(side)} | {detail}")

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
        }
        trades.append(trade)
        events.append(f"[{ts.strftime('%Y-%m-%d %H:%M')}] EXIT {_side_label(pos.side)} {reason} | PnL={net:,.2f}")
        pos = OpenPosition()
        closed_this_bar = True

    def open_position(ts: pd.Timestamp, raw_px: float, side: int, row: pd.Series, equity: float) -> None:
        nonlocal cash, pos
        if equity <= 0:
            add_skip(ts, side, "no_equity", f"Equity={equity:,.2f}; no capital available.")
            return
        entry_px = _effective_price(float(raw_px), side, "entry", cfg)
        stop_hint = row.get("stop_px", np.nan)
        target_hint = row.get("tp2_px", row.get("target_px", np.nan))
        stop_valid = (
            np.isfinite(stop_hint)
            and ((side == LONG and float(stop_hint) < entry_px) or (side == SHORT and float(stop_hint) > entry_px))
        )
        target_valid = (
            np.isfinite(target_hint)
            and ((side == LONG and float(target_hint) > entry_px) or (side == SHORT and float(target_hint) < entry_px))
        )
        if np.isfinite(stop_hint) and not stop_valid:
            add_skip(
                ts,
                side,
                "invalid_stop",
                f"Fill price={entry_px:,.4f} crossed planned stop={float(stop_hint):,.4f}; setup invalid.",
            )
            return
        if stop_valid:
            stop_px = float(stop_hint)
            target_px = float(target_hint) if target_valid else derive_brackets(side, entry_px, stop_px, row.get("atr", np.nan))[1]
            init_risk = risk_per_unit(entry_px, stop_px)
        else:
            stop_anchor = float(stop_hint) if np.isfinite(stop_hint) else float(row.get("st_line", np.nan))
            stop_px, target_px, init_risk = derive_brackets(side, entry_px, stop_anchor, float(row.get("atr", np.nan)))
            if target_valid:
                target_px = float(target_hint)
        qty, init_risk, risk_amount = _size_position(entry_px, stop_px, equity, cfg)
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
        )
        events.append(
            f"[{ts.strftime('%Y-%m-%d %H:%M')}] ENTRY {_side_label(side)} | "
            f"Entry={entry_px:,.4f} Qty={qty:,.8f} Notional={notional:,.2f} "
            f"Lev={leverage_used:.2f}x/{cfg.leverage:.2f}x"
        )

    def update_trailing_stop(ts: pd.Timestamp, row: pd.Series) -> None:
        close_px = float(row.get("Close", np.nan))
        if pos.side == LONG and np.isfinite(row.get("st_line", np.nan)):
            next_stop = max(pos.stop_px, float(row.get("st_line")))
            if np.isfinite(close_px) and next_stop < close_px and next_stop > pos.stop_px + 1e-9:
                pos.stop_px = next_stop
                events.append(f"[{ts.strftime('%Y-%m-%d %H:%M')}] TRAIL LONG SL -> {pos.stop_px:,.4f}")
        elif pos.side == SHORT and np.isfinite(row.get("st_line", np.nan)):
            next_stop = min(pos.stop_px, float(row.get("st_line")))
            if np.isfinite(close_px) and next_stop > close_px and next_stop < pos.stop_px - 1e-9:
                pos.stop_px = next_stop
                events.append(f"[{ts.strftime('%Y-%m-%d %H:%M')}] TRAIL SHORT SL -> {pos.stop_px:,.4f}")

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

    rows = list(data.iterrows())
    for i, (ts, row) in enumerate(rows):
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
                    open_position(ts, raw_open, reverse_side, pending_reverse["row"], cash)
                pending_reverse = None
                pending_entry = None
            if pending_entry and pos.side == FLAT and not closed_this_bar:
                entry_side = int(pending_entry["side"])
                if entry_side == SHORT and not cfg.allow_shorts:
                    add_skip(ts, entry_side, "shorts_disabled", f"SHORT ignored in {cfg.mode.upper()} mode.")
                else:
                    open_position(ts, raw_open, entry_side, pending_entry["row"], current_equity)
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

        if pos.side == FLAT and not pending_exit and not closed_this_bar:
            side = int(row.get("entry_side", 0) or 0)
            if side in (LONG, SHORT):
                if cfg.fill_model == "close":
                    if side == SHORT and not cfg.allow_shorts:
                        add_skip(ts, side, "shorts_disabled", f"SHORT ignored in {cfg.mode.upper()} mode.")
                    else:
                        open_position(ts, raw_close, side, row, current_equity)
                elif i < len(rows) - 1:
                    pending_entry = {"side": side, "row": row.copy()}
                else:
                    add_skip(ts, side, "no_next_bar", "No next candle available for next-open fill.")

        mark_equity(ts, raw_close)

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
    summary = compute_summary(equity, trades, cfg.initial_capital, bars_per_year)
    summary["skipped_signals"] = sum(skip_counts.values())
    summary["skip_counts"] = dict(sorted(skip_counts.items()))
    summary["max_leverage"] = cfg.leverage

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
        "events": events[-200:],
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
        },
    }


def naive_trade_row(trade: dict) -> dict:
    out = dict(trade)
    out["entry_ts"] = _to_naive(pd.Timestamp(out["entry_ts"])) if out.get("entry_ts") else None
    out["exit_ts"] = _to_naive(pd.Timestamp(out["exit_ts"])) if out.get("exit_ts") else None
    return out


def naive_equity_ts(value: str) -> pd.Timestamp:
    return _to_naive(pd.Timestamp(value)) or pd.Timestamp.utcnow().tz_localize(None)
