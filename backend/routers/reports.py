from __future__ import annotations

import asyncio
import os
from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
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


class SendEmailRequest(BaseModel):
    to: str


def _trade_dicts(trades) -> list[dict]:
    return [_trade_row(t) for t in trades]


def _parse_emails(raw: str) -> list[str]:
    return [addr.strip() for addr in raw.split(",") if addr.strip() and "@" in addr.strip()]


async def dispatch_report(to_emails: list[str]) -> dict:
    """Core report dispatch — shared by the API endpoint and the daily scheduler."""
    now = datetime.utcnow()

    async with AsyncSessionLocal() as db:
        paper_result = await db.execute(
            select(Trade).where(Trade.execution_mode == "paper").order_by(Trade.entry_ts.asc())
        )
        paper_trades_orm = paper_result.scalars().all()
        real_result = await db.execute(
            select(Trade).where(Trade.execution_mode == "real").order_by(Trade.entry_ts.asc())
        )
        real_trades_orm = real_result.scalars().all()

    paper_dicts = _trade_dicts(paper_trades_orm)
    real_dicts = _trade_dicts(real_trades_orm)

    def _build_and_send():
        from ..services.report_pdf import generate_pdf
        from ..services.email_sender import send_report_email
        p_pdf = generate_pdf(paper_dicts, "Paper Trade Report", now) if paper_dicts else None
        r_pdf = generate_pdf(real_dicts, "Real Trade Report", now) if real_dicts else None
        send_report_email(
            to_emails=to_emails,
            paper_pdf=p_pdf,
            real_pdf=r_pdf,
            paper_count=len(paper_dicts),
            real_count=len(real_dicts),
            generated_at=now,
        )
        return p_pdf, r_pdf

    paper_pdf, real_pdf = await asyncio.to_thread(_build_and_send)

    attachments = []
    if paper_pdf:
        attachments.append("paper_trades.pdf")
    if real_pdf:
        attachments.append("real_trades.pdf")

    recipients = ", ".join(to_emails)
    msg = f"Report sent to {recipients}."
    if attachments:
        msg += f" Attached: {', '.join(attachments)}."
    else:
        msg += " No trades recorded yet — summary sent in email body."

    return {"ok": True, "message": msg, "paper_count": len(paper_dicts), "real_count": len(real_dicts)}


@router.post("/api/reports/send-email")
async def send_email_report(body: SendEmailRequest) -> JSONResponse:
    to_emails = _parse_emails(body.to)
    if not to_emails:
        return JSONResponse({"ok": False, "message": "No valid email address provided."}, status_code=400)
    try:
        result = await dispatch_report(to_emails)
        return JSONResponse(result)
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=500)
    except Exception as exc:
        return JSONResponse({"ok": False, "message": f"Failed: {exc}"}, status_code=500)
