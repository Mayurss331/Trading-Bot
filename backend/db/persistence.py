from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .database import AsyncSessionLocal
from .models import AccountSnapshot, PaperAccount, PaperOrder, PaperOrderEvent, PositionSnapshot, SignalEvent, Trade, UserSetting


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _jsonable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "isoformat") and value.__class__.__module__.startswith("pandas"):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def current_month_key(now: datetime | None = None) -> str:
    value = now or datetime.utcnow()
    return value.strftime("%Y-%m")


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
        entry_ts = t.get("entry_ts")
        side = int(t.get("side") or 0)
        pair = str(t.get("pair") or "")
        strategy = t.get("strategy")
        execution_mode = str(t.get("execution_mode") or "paper")

        existing = None
        if entry_ts and pair and side:
            result = await db.execute(
                select(Trade)
                .where(
                    Trade.pair == pair,
                    Trade.side == side,
                    Trade.entry_ts == entry_ts,
                    Trade.strategy == strategy,
                    Trade.execution_mode == execution_mode,
                )
                .order_by(Trade.id.desc())
                .limit(1)
            )
            existing = result.scalar_one_or_none()

        values = dict(
            pair=str(t.get("pair") or ""),
            side=side,
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
            strategy=strategy,
            execution_mode=execution_mode,
        )
        if existing is not None:
            for key, value in values.items():
                setattr(existing, key, value)
        else:
            db.add(Trade(**values))
        await db.commit()


async def get_or_create_paper_account(
    *,
    strategy_key: str,
    strategy: str,
    custom_strategy_id: int | None = None,
    starting_capital: float = 100.0,
    month_key: str | None = None,
) -> dict:
    month = month_key or current_month_key()
    capital = max(1.0, float(starting_capital or 100.0))
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(PaperAccount).where(
                PaperAccount.month_key == month,
                PaperAccount.strategy_key == strategy_key,
            )
        )
        account = result.scalar_one_or_none()
        if account is None:
            account = PaperAccount(
                month_key=month,
                strategy_key=strategy_key,
                strategy=strategy,
                custom_strategy_id=custom_strategy_id,
                starting_capital=capital,
                equity=capital,
            )
            db.add(account)
            await db.commit()
            await db.refresh(account)
        return _paper_account_payload(account)


def _paper_account_payload(account: PaperAccount) -> dict:
    return {
        "id": account.id,
        "month_key": account.month_key,
        "strategy_key": account.strategy_key,
        "strategy": account.strategy,
        "custom_strategy_id": account.custom_strategy_id,
        "starting_capital": account.starting_capital,
        "realized_pnl": account.realized_pnl,
        "unrealized_pnl": account.unrealized_pnl,
        "equity": account.equity,
        "open_positions": account.open_positions,
        "closed_trades": account.closed_trades,
    }


async def paper_account_snapshot(account_id: int) -> dict | None:
    async with AsyncSessionLocal() as db:
        account = await db.get(PaperAccount, account_id)
        return _paper_account_payload(account) if account else None


async def paper_account_capacity(account_id: int) -> dict | None:
    async with AsyncSessionLocal() as db:
        account = await db.get(PaperAccount, account_id)
        if account is None:
            return None
        reserved_result = await db.execute(
            select(func.coalesce(func.sum(PaperOrder.risk_usd), 0.0)).where(
                PaperOrder.account_id == account_id,
                PaperOrder.status == "open",
            )
        )
        reserved = float(reserved_result.scalar() or 0.0)
        equity = float(account.equity or 0.0)
        return {
            **_paper_account_payload(account),
            "reserved_risk": reserved,
            "available_risk": max(0.0, equity - reserved),
        }


async def _recalculate_paper_account(db, account: PaperAccount) -> None:
    closed_pnl_result = await db.execute(
        select(func.coalesce(func.sum(PaperOrder.realized_pnl), 0.0)).where(
            PaperOrder.account_id == account.id,
            PaperOrder.status == "closed",
        )
    )
    open_unrealized_result = await db.execute(
        select(func.coalesce(func.sum(PaperOrder.unrealized_pnl), 0.0)).where(
            PaperOrder.account_id == account.id,
            PaperOrder.status == "open",
        )
    )
    open_count_result = await db.execute(
        select(func.count()).select_from(PaperOrder).where(
            PaperOrder.account_id == account.id,
            PaperOrder.status == "open",
        )
    )
    closed_count_result = await db.execute(
        select(func.count()).select_from(PaperOrder).where(
            PaperOrder.account_id == account.id,
            PaperOrder.status == "closed",
        )
    )
    realized = float(closed_pnl_result.scalar() or 0.0)
    unrealized = float(open_unrealized_result.scalar() or 0.0)
    account.realized_pnl = realized
    account.unrealized_pnl = unrealized
    account.equity = float(account.starting_capital or 0.0) + realized + unrealized
    account.open_positions = int(open_count_result.scalar() or 0)
    account.closed_trades = int(closed_count_result.scalar() or 0)
    account.updated_at = datetime.utcnow()


async def store_paper_order(account_id: int, trade: dict, *, strategy_key: str = "") -> dict | None:
    async with AsyncSessionLocal() as db:
        account = await db.get(PaperAccount, account_id)
        if account is None:
            return None

        pair = str(trade.get("pair") or "")
        side = int(trade.get("side") or 0)
        entry_ts = trade.get("entry_ts")
        if not pair or not side or not entry_ts:
            return None

        result = await db.execute(
            select(PaperOrder).where(
                PaperOrder.account_id == account_id,
                PaperOrder.pair == pair,
                PaperOrder.side == side,
                PaperOrder.entry_ts == entry_ts,
            )
        )
        order = result.scalar_one_or_none()
        is_new = order is None
        status = "closed" if trade.get("exit_ts") else "open"
        realized = _float_or_none(trade.get("pnl")) or 0.0

        values = {
            "account_id": account_id,
            "pair": pair,
            "market": trade.get("market"),
            "coin": trade.get("coin"),
            "strategy": str(trade.get("strategy") or account.strategy),
            "custom_strategy_id": account.custom_strategy_id,
            "timeframe": trade.get("timeframe"),
            "side": side,
            "status": status,
            "entry_ts": entry_ts,
            "exit_ts": trade.get("exit_ts"),
            "entry_px": float(trade.get("entry_px") or 0.0),
            "exit_px": _float_or_none(trade.get("exit_px")),
            "stop_px": _float_or_none(trade.get("stop_px")),
            "target_px": _float_or_none(trade.get("target_px")),
            "qty": float(trade.get("qty") or 0.0),
            "risk_usd": float(trade.get("risk_usd") or 0.0),
            "realized_pnl": realized if status == "closed" else 0.0,
            "unrealized_pnl": 0.0,
            "exit_reason": trade.get("exit_reason"),
            "payload": _jsonable({**trade, "strategy_key": strategy_key or account.strategy_key}),
            "updated_at": datetime.utcnow(),
        }
        if order is None:
            order = PaperOrder(**values)
            db.add(order)
            await db.flush()
        else:
            for key, value in values.items():
                setattr(order, key, value)

        event_type = "entry" if is_new and status == "open" else ("exit" if status == "closed" else "update")
        db.add(PaperOrderEvent(
            order_id=order.id,
            account_id=account.id,
            event_type=event_type,
            message=f"{event_type.upper()} {pair} {account.strategy}",
            payload=_jsonable(values),
        ))
        await _recalculate_paper_account(db, account)
        await db.commit()
        await db.refresh(order)
        await db.refresh(account)
        return {
            "order_id": order.id,
            "account": _paper_account_payload(account),
            "status": order.status,
        }


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
