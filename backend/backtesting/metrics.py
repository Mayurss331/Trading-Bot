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


def _max_consecutive(flags: list[bool]) -> int:
    best = cur = 0
    for f in flags:
        cur = cur + 1 if f else 0
        if cur > best:
            best = cur
    return best


def monte_carlo_drawdown(
    trade_returns: list[float],
    n_simulations: int = 1000,
    seed: int = 42,
) -> dict:
    """Bootstrap-resample per-trade % returns → drawdown confidence intervals."""
    if len(trade_returns) < 5:
        return {}
    rng = np.random.default_rng(seed)
    arr = np.array(trade_returns, dtype=float)
    max_dds: list[float] = []
    final_rets: list[float] = []
    for _ in range(n_simulations):
        sim = rng.choice(arr, size=len(arr), replace=True)
        equity = (1.0 + sim).cumprod()
        peak = np.maximum.accumulate(equity)
        with np.errstate(invalid="ignore", divide="ignore"):
            dd = np.where(peak > 0, (equity - peak) / peak, 0.0)
        max_dds.append(float(np.nanmin(dd)))
        final_rets.append(float(equity[-1] - 1.0))
    mdd = np.array(max_dds)
    fr = np.array(final_rets)
    return {
        "mc_max_dd_median_pct": _round(float(np.median(mdd)) * 100, 2),
        "mc_max_dd_p95_pct": _round(float(np.percentile(mdd, 5)) * 100, 2),
        "mc_max_dd_worst_pct": _round(float(mdd.min()) * 100, 2),
        "mc_final_return_median_pct": _round(float(np.median(fr)) * 100, 2),
        "mc_prob_loss_pct": _round(float((fr < 0).mean()) * 100, 2),
    }


def compute_summary(
    equity: pd.DataFrame,
    trades: list[dict],
    initial_capital: float,
    bars_per_year: float,
    risk_free_rate: float = 0.0,
) -> dict:
    if equity.empty:
        return {
            "initial_capital": initial_capital,
            "final_equity": initial_capital,
            "total_return_pct": 0.0,
            "trades": 0,
        }

    eq = equity["equity"].astype(float)
    all_rets = eq.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    final_equity = float(eq.iloc[-1])
    total_return = (final_equity / initial_capital - 1.0) if initial_capital else 0.0

    start = equity.index.min()
    end = equity.index.max()
    years = (
        max((end - start).total_seconds() / (365.25 * 24 * 3600), 1 / 365.25)
        if isinstance(start, pd.Timestamp)
        else 0
    )
    cagr = (
        (final_equity / initial_capital) ** (1 / years) - 1
        if years > 0 and initial_capital > 0 and final_equity > 0
        else None
    )

    # Use only bars where a position was open — excludes idle cash which deflates vol
    rf_bar = risk_free_rate / bars_per_year if bars_per_year > 0 else 0.0
    if "position_value" in equity.columns:
        active_mask = equity["position_value"].reindex(all_rets.index).fillna(0) > 0
        active_rets = all_rets[active_mask]
    else:
        active_rets = all_rets
    rets = active_rets if len(active_rets) > 10 else all_rets

    vol = float(rets.std() * math.sqrt(bars_per_year)) if len(rets) > 1 else None
    excess_mean = (rets.mean() - rf_bar) * bars_per_year
    sharpe = float(excess_mean / vol) if vol and vol > 0 else None

    down = rets[rets < 0]
    down_vol = float(down.std() * math.sqrt(bars_per_year)) if len(down) > 1 else None
    sortino = float(excess_mean / down_vol) if down_vol and down_vol > 0 else None

    roll_max = eq.cummax()
    dd = (eq - roll_max) / roll_max.replace(0, np.nan)
    max_dd = float(dd.min()) if not dd.empty else 0.0
    calmar = float(cagr / abs(max_dd)) if cagr is not None and max_dd < 0 else None

    # Ulcer index: sqrt(mean(dd²)) — penalises prolonged drawdowns more than max_dd alone
    ulcer_index = float(np.sqrt(np.mean(dd.fillna(0).values ** 2)) * 100) if not dd.empty else None

    # ── Trade-level stats ──────────────────────────────────────────────
    closed = [t for t in trades if t.get("exit_ts")]
    pnls = [float(t.get("net_pnl") or 0.0) for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    durations = [
        float(t.get("duration_minutes") or 0.0)
        for t in closed
        if t.get("duration_minutes") is not None
    ]

    total_trades = len(pnls)
    avg_win = float(np.mean(wins)) if wins else 0.0
    avg_loss = float(np.mean(losses)) if losses else 0.0
    win_rate = len(wins) / total_trades if total_trades > 0 else 0.0
    # Expectancy: expected $ per trade
    expectancy = win_rate * avg_win + (1.0 - win_rate) * avg_loss

    # VaR / CVaR (95%) on trade P&L distribution
    var_95 = cvar_95 = None
    if len(pnls) >= 10:
        pnl_arr = np.array(pnls)
        var_95 = float(np.percentile(pnl_arr, 5))
        below = pnl_arr[pnl_arr <= var_95]
        cvar_95 = float(below.mean()) if len(below) > 0 else var_95

    # Long / Short split
    long_closed = [t for t in closed if t.get("side") == "LONG"]
    short_closed = [t for t in closed if t.get("side") == "SHORT"]
    long_pnls = [float(t.get("net_pnl") or 0.0) for t in long_closed]
    short_pnls = [float(t.get("net_pnl") or 0.0) for t in short_closed]
    long_wins = [p for p in long_pnls if p > 0]
    short_wins = [p for p in short_pnls if p > 0]

    # Consecutive streaks
    win_flags = [p > 0 for p in pnls]
    loss_flags = [p <= 0 for p in pnls]
    max_consec_wins = _max_consecutive(win_flags)
    max_consec_losses = _max_consecutive(loss_flags)

    # Monte Carlo — use per-trade % returns (at least 10 trades required)
    mc: dict = {}
    if len(pnls) >= 10:
        ret_pcts = [
            float(t.get("return_pct") or 0.0) / 100.0
            for t in closed
            if t.get("return_pct") is not None
        ]
        if len(ret_pcts) >= 10:
            mc = monte_carlo_drawdown(ret_pcts)

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
        "ulcer_index": _round(ulcer_index, 3),
        "trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": _round(win_rate * 100, 2),
        "net_pnl": _round(sum(pnls), 2),
        "gross_profit": _round(gross_profit, 2),
        "gross_loss": _round(gross_loss, 2),
        "profit_factor": _round((gross_profit / gross_loss) if gross_loss > 0 else None, 3),
        "expectancy": _round(expectancy, 2),
        "avg_win": _round(avg_win if wins else None, 2),
        "avg_loss": _round(avg_loss if losses else None, 2),
        "largest_win": _round(max(wins) if wins else None, 2),
        "largest_loss": _round(min(losses) if losses else None, 2),
        "avg_duration_minutes": _round(float(np.mean(durations)) if durations else None, 2),
        "max_consecutive_wins": max_consec_wins,
        "max_consecutive_losses": max_consec_losses,
        "var_95": _round(var_95, 2),
        "cvar_95": _round(cvar_95, 2),
        "long_trades": len(long_closed),
        "long_win_rate_pct": _round((len(long_wins) / len(long_pnls) * 100) if long_pnls else None, 2),
        "short_trades": len(short_closed),
        "short_win_rate_pct": _round((len(short_wins) / len(short_pnls) * 100) if short_pnls else None, 2),
        **mc,
    }
