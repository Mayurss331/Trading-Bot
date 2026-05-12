from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import select

from ..db.database import AsyncSessionLocal
from ..db.models import Trade
from ..utils import clean

router = APIRouter(tags=["reports"])


def _trade_row(t: Trade) -> dict:
    return {
        "id": t.id,
        "pair": t.pair,
        "side": t.side,
        "entry_ts": t.entry_ts.isoformat() if t.entry_ts else None,
        "exit_ts": t.exit_ts.isoformat() if t.exit_ts else None,
        "entry_px": t.entry_px,
        "exit_px": t.exit_px,
        "stop_px": t.stop_px,
        "target_px": t.target_px,
        "qty": t.qty,
        "risk_usd": t.risk_usd,
        "pnl": t.pnl,
        "exit_reason": t.exit_reason,
        "mode": t.mode,
        "strategy": t.strategy,
        "execution_mode": t.execution_mode,
    }


@router.get("/api/reports/trades")
async def report_trades(
    exec_mode: str = "all",
    pair: str = "",
    date_from: str = "",
    date_to: str = "",
    limit: int = 100,
    offset: int = 0,
) -> JSONResponse:
    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    stmt = select(Trade)
    if exec_mode in {"paper", "real"}:
        stmt = stmt.where(Trade.execution_mode == exec_mode)
    if pair:
        stmt = stmt.where(Trade.pair == pair)
    if date_from:
        try:
            stmt = stmt.where(Trade.entry_ts >= datetime.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            stmt = stmt.where(Trade.entry_ts <= datetime.fromisoformat(date_to))
        except ValueError:
            pass
    stmt = stmt.order_by(Trade.entry_ts.desc()).limit(limit).offset(offset)

    async with AsyncSessionLocal() as db:
        result = await db.execute(stmt)
        trades = result.scalars().all()

    return JSONResponse({"ok": True, "count": len(trades), "trades": [_trade_row(t) for t in trades]})


@router.get("/api/reports/overview")
async def report_overview(exec_mode: str = "all") -> JSONResponse:
    stmt = select(Trade).where(Trade.exit_ts.isnot(None))
    if exec_mode in {"paper", "real"}:
        stmt = stmt.where(Trade.execution_mode == exec_mode)

    async with AsyncSessionLocal() as db:
        result = await db.execute(stmt)
        trades = result.scalars().all()

    if not trades:
        return JSONResponse({
            "ok": True, "exec_mode": exec_mode,
            "total_trades": 0, "wins": 0, "losses": 0, "win_rate": None,
            "net_pnl": 0.0, "profit_factor": None, "max_drawdown": None,
            "avg_duration_minutes": None,
        })

    pnls = [t.pnl for t in trades if t.pnl is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    net_pnl = sum(pnls)
    win_rate = len(wins) / len(pnls) * 100 if pnls else None
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

    # Max drawdown from equity curve
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    durations = []
    for t in trades:
        if t.entry_ts and t.exit_ts:
            durations.append((t.exit_ts - t.entry_ts).total_seconds() / 60)
    avg_duration = sum(durations) / len(durations) if durations else None

    return JSONResponse(clean({
        "ok": True,
        "exec_mode": exec_mode,
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "net_pnl": net_pnl,
        "profit_factor": profit_factor,
        "max_drawdown": max_dd if max_dd > 0 else 0.0,
        "avg_duration_minutes": avg_duration,
    }))


@router.get("/api/reports/pnl-series")
async def report_pnl_series(exec_mode: str = "all") -> JSONResponse:
    stmt = select(Trade).where(Trade.exit_ts.isnot(None), Trade.pnl.isnot(None))
    if exec_mode in {"paper", "real"}:
        stmt = stmt.where(Trade.execution_mode == exec_mode)
    stmt = stmt.order_by(Trade.exit_ts.asc())

    async with AsyncSessionLocal() as db:
        result = await db.execute(stmt)
        trades = result.scalars().all()

    cumulative = 0.0
    series: list[dict] = []
    for t in trades:
        cumulative += t.pnl or 0.0
        date_str = t.exit_ts.strftime("%Y-%m-%d") if t.exit_ts else None
        if series and series[-1]["date"] == date_str:
            series[-1]["cumulative_pnl"] = cumulative
            series[-1]["daily_pnl"] += t.pnl or 0.0
        else:
            series.append({
                "date": date_str,
                "daily_pnl": t.pnl or 0.0,
                "cumulative_pnl": cumulative,
            })

    return JSONResponse({"ok": True, "exec_mode": exec_mode, "series": series})
