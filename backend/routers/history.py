from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import select

from ..db.database import AsyncSessionLocal
from ..db.models import AccountSnapshot, SignalEvent

router = APIRouter(tags=["history"])


@router.get("/api/signals")
async def signals(pair: str = "", strategy: str = "", limit: int = 100) -> JSONResponse:
    limit = max(1, min(limit, 500))
    stmt = select(SignalEvent)
    if pair:
        stmt = stmt.where(SignalEvent.pair == pair)
    if strategy:
        stmt = stmt.where(SignalEvent.strategy == strategy)
    stmt = stmt.order_by(SignalEvent.ts.desc()).limit(limit)

    async with AsyncSessionLocal() as db:
        result = await db.execute(stmt)
        rows = result.scalars().all()

    return JSONResponse({
        "ok": True,
        "count": len(rows),
        "signals": [
            {
                "id": row.id,
                "ts": row.ts.isoformat() if row.ts else None,
                "pair": row.pair,
                "market": row.market,
                "coin": row.coin,
                "strategy": row.strategy,
                "mode": row.mode,
                "price": row.price,
                "score": row.score,
                "rsi": row.rsi,
                "action_type": row.action_type,
                "side": row.side,
                "signal": row.signal,
                "reason": row.reason,
                "freshness_minutes": row.freshness_minutes,
            }
            for row in rows
        ],
    })


@router.get("/api/account-snapshots")
async def account_snapshots(limit: int = 100) -> JSONResponse:
    limit = max(1, min(limit, 500))
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(AccountSnapshot).order_by(AccountSnapshot.ts.desc()).limit(limit)
        )
        rows = result.scalars().all()

    return JSONResponse({
        "ok": True,
        "count": len(rows),
        "snapshots": [
            {
                "id": row.id,
                "ts": row.ts.isoformat() if row.ts else None,
                "mode": row.mode,
                "currency": row.currency,
                "available_quote_balance": row.available_quote_balance,
                "total_unrealized_pnl": row.total_unrealized_pnl,
                "positions_count": row.positions_count,
            }
            for row in rows
        ],
    })
