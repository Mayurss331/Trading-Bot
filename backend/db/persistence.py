from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .database import AsyncSessionLocal
from .models import AccountSnapshot, PositionSnapshot, SignalEvent, Trade, UserSetting


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


async def save_user_setting(key: str, payload: dict) -> None:
    async with AsyncSessionLocal() as db:
        stmt = (
            sqlite_insert(UserSetting)
            .values(key=key, payload=payload, updated_at=datetime.utcnow())
            .on_conflict_do_update(
                index_elements=["key"],
                set_={"payload": payload, "updated_at": datetime.utcnow()},
            )
        )
        await db.execute(stmt)
        await db.commit()


async def store_trade(t: dict) -> None:
    async with AsyncSessionLocal() as db:
        db.add(Trade(
            pair=str(t.get("pair") or ""),
            side=int(t.get("side") or 0),
            entry_ts=t.get("entry_ts"),
            exit_ts=t.get("exit_ts"),
            entry_px=float(t.get("entry_px") or 0.0),
            exit_px=_float_or_none(t.get("exit_px")),
            stop_px=float(t.get("stop_px") or 0.0),
            target_px=float(t.get("target_px") or 0.0),
            qty=float(t.get("qty") or 0.0),
            risk_usd=float(t.get("risk_usd") or 0.0),
            pnl=_float_or_none(t.get("pnl")),
            exit_reason=t.get("exit_reason"),
            mode=t.get("mode"),
            strategy=t.get("strategy"),
            execution_mode=str(t.get("execution_mode") or "paper"),
        ))
        await db.commit()


async def store_signal_event(payload: dict) -> None:
    if not payload.get("ok"):
        return

    strategy = payload.get("strategy") or {}
    stats = payload.get("stats") or {}
    action = payload.get("action") or {}

    row = SignalEvent(
        pair=str(payload.get("pair") or ""),
        market=payload.get("market"),
        coin=payload.get("coin"),
        strategy=strategy.get("id"),
        mode=payload.get("mode"),
        price=_float_or_none(stats.get("close") or (payload.get("ticker") or {}).get("last_price")),
        score=_float_or_none(stats.get("score")),
        rsi=_float_or_none(stats.get("rsi")),
        action_type=action.get("type"),
        side=action.get("side"),
        signal=action.get("label"),
        reason=strategy.get("reason"),
        freshness_minutes=_float_or_none(payload.get("freshness_minutes")),
        payload=payload,
    )
    async with AsyncSessionLocal() as db:
        db.add(row)
        await db.commit()


async def store_tracker_signal_events(rows: list[dict]) -> None:
    async with AsyncSessionLocal() as db:
        for item in rows:
            if not item.get("ok"):
                continue
            db.add(SignalEvent(
                pair=str(item.get("pair") or ""),
                market=item.get("market"),
                coin=item.get("coin"),
                strategy=item.get("strategy"),
                mode="futures",
                price=_float_or_none(item.get("last_price")),
                score=_float_or_none(item.get("score")),
                rsi=_float_or_none(item.get("rsi")),
                action_type=item.get("action_type"),
                side=item.get("side"),
                signal=item.get("signal"),
                reason=None,
                freshness_minutes=_float_or_none(item.get("freshness_minutes")),
                payload=item,
            ))
        await db.commit()


async def store_account_snapshot(payload: dict) -> None:
    if not payload.get("has_credentials"):
        return

    positions = payload.get("positions") or []
    total_pnl = sum(_float_or_none(pos.get("unrealized_pnl")) or 0.0 for pos in positions)
    snapshot = AccountSnapshot(
        mode=str(payload.get("mode") or "futures"),
        currency=payload.get("currency"),
        available_quote_balance=_float_or_none(payload.get("available_quote_balance")),
        total_unrealized_pnl=total_pnl,
        positions_count=len(positions),
        payload=payload,
    )
    async with AsyncSessionLocal() as db:
        db.add(snapshot)
        await db.flush()
        for pos in positions:
            db.add(PositionSnapshot(
                account_snapshot_id=snapshot.id,
                pair=pos.get("pair"),
                coin=pos.get("coin"),
                side=pos.get("side"),
                quantity=_float_or_none(pos.get("quantity")),
                avg_price=_float_or_none(pos.get("avg_price")),
                mark_price=_float_or_none(pos.get("mark_price")),
                liquidation_price=_float_or_none(pos.get("liquidation_price")),
                stop_loss_trigger=_float_or_none(pos.get("stop_loss_trigger")),
                take_profit_trigger=_float_or_none(pos.get("take_profit_trigger")),
                unrealized_pnl=_float_or_none(pos.get("unrealized_pnl")),
                margin=_float_or_none(pos.get("margin")),
                leverage=_float_or_none(pos.get("leverage")),
                payload=pos,
            ))
        await db.commit()
