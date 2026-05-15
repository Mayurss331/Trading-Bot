from __future__ import annotations

import math

import numpy as np
import pandas as pd


def _round(value: float | None, ndigits: int = 4) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return round(f, ndigits)


def compute_summary(equity: pd.DataFrame, trades: list[dict], initial_capital: float, bars_per_year: float) -> dict:
    if equity.empty:
        return {
            "initial_capital": initial_capital,
            "final_equity": initial_capital,
            "total_return_pct": 0.0,
            "trades": 0,
        }

    eq = equity["equity"].astype(float)
    rets = eq.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    final_equity = float(eq.iloc[-1])
    total_return = (final_equity / initial_capital - 1.0) if initial_capital else 0.0

    start = equity.index.min()
    end = equity.index.max()
    years = max((end - start).total_seconds() / (365.25 * 24 * 3600), 1 / 365.25) if isinstance(start, pd.Timestamp) else 0
    cagr = (final_equity / initial_capital) ** (1 / years) - 1 if years > 0 and initial_capital > 0 and final_equity > 0 else None

    vol = float(rets.std() * math.sqrt(bars_per_year)) if len(rets) > 1 else None
    sharpe = float((rets.mean() * bars_per_year) / vol) if vol and vol > 0 else None
    down = rets[rets < 0]
    down_vol = float(down.std() * math.sqrt(bars_per_year)) if len(down) > 1 else None
    sortino = float((rets.mean() * bars_per_year) / down_vol) if down_vol and down_vol > 0 else None

    roll_max = eq.cummax()
    dd = (eq - roll_max) / roll_max.replace(0, np.nan)
    max_dd = float(dd.min()) if not dd.empty else 0.0
    calmar = float(cagr / abs(max_dd)) if cagr is not None and max_dd < 0 else None

    closed = [t for t in trades if t.get("exit_ts")]
    pnls = [float(t.get("net_pnl") or 0.0) for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    durations = [float(t.get("duration_minutes") or 0.0) for t in closed if t.get("duration_minutes") is not None]

    return {
        "initial_capital": _round(initial_capital, 2),
        "final_equity": _round(final_equity, 2),
        "total_return_pct": _round(total_return * 100, 2),
        "cagr_pct": _round(cagr * 100 if cagr is not None else None, 2),
        "volatility_pct": _round(vol * 100 if vol is not None else None, 2),
        "sharpe": _round(sharpe, 3),
        "sortino": _round(sortino, 3),
        "max_drawdown_pct": _round(max_dd * 100, 2),
        "calmar": _round(calmar, 3),
        "trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": _round((len(wins) / len(pnls) * 100) if pnls else None, 2),
        "net_pnl": _round(sum(pnls), 2),
        "gross_profit": _round(gross_profit, 2),
        "gross_loss": _round(gross_loss, 2),
        "profit_factor": _round((gross_profit / gross_loss) if gross_loss > 0 else None, 3),
        "avg_win": _round(float(np.mean(wins)) if wins else None, 2),
        "avg_loss": _round(float(np.mean(losses)) if losses else None, 2),
        "largest_win": _round(max(wins) if wins else None, 2),
        "largest_loss": _round(min(losses) if losses else None, 2),
        "avg_duration_minutes": _round(float(np.mean(durations)) if durations else None, 2),
    }

