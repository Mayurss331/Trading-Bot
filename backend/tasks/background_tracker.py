"""APScheduler job: keep saved futures tracker coins scanned without a browser."""
from __future__ import annotations

import asyncio
import logging
import os

import pandas as pd
from sqlalchemy import select

from ..bot_loader import bot
from ..db.database import AsyncSessionLocal
from ..db.models import UserSetting
from ..db.persistence import store_tracker_signal_events
from ..utils import clean, coin_from_pair, futures_market_for_coin, futures_pair_for_coin, make_cfg

logger = logging.getLogger(__name__)

_EXECUTION_STATE: dict[str, dict[str, object]] = {}


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


async def _load_dashboard_settings() -> dict:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(UserSetting).where(UserSetting.key == "dashboard"))
        row = result.scalar_one_or_none()
    return row.payload if row and isinstance(row.payload, dict) else {}


def _normalise_coins(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    coins: list[str] = []
    for item in raw:
        text = str(item).strip().upper()
        if not text:
            continue
        coin = coin_from_pair(text) if ("_" in text or "-" in text) else "".join(ch for ch in text if ch.isalnum())
        if coin and coin not in coins:
            coins.append(coin)
        if len(coins) >= 12:
            break
    return coins


def _snapshot_one(coin: str, strategy_id: str, risk: float, lookback_days: int, timeframe: str | None = None) -> dict:
    # Import here to avoid making router import order part of application startup.
    from ..routers.snapshot import _sync_build_snapshot

    pair = futures_pair_for_coin(coin)
    market = futures_market_for_coin(coin)
    snap = _sync_build_snapshot(pair, market, strategy_id, "futures", lookback_days, 80, risk, timeframe=timeframe)
    strategy_info = snap.get("strategy") or {}
    stats = snap.get("stats") or {}
    action = snap.get("action") or {}
    state = snap.get("state") or {}
    ticker = snap.get("ticker") or {}
    return clean({
        "ok": bool(snap.get("ok")),
        "coin": snap.get("coin") or coin,
        "pair": snap.get("pair") or pair,
        "market": snap.get("market") or market,
        "strategy": strategy_info.get("id"),
        "strategy_name": strategy_info.get("name"),
        "last_price": ticker.get("last_price") or stats.get("close"),
        "bid": ticker.get("bid"),
        "ask": ticker.get("ask"),
        "score": stats.get("score"),
        "rsi": stats.get("rsi"),
        "signal": action.get("label"),
        "action_type": action.get("type"),
        "side": action.get("side"),
        "position": state.get("side"),
        "pnl": state.get("realized_pnl"),
        "freshness_minutes": snap.get("freshness_minutes"),
        "latest_time": stats.get("latest_time"),
        "server_time": pd.Timestamp.now(tz=bot.IST),
        "source": "background_tracker",
    })


def _execute_one(coin: str, risk: float, lookback_days: int, timeframe: str | None = None) -> list[str]:
    pair = futures_pair_for_coin(coin)
    market = futures_market_for_coin(coin)
    cfg = make_cfg(pair, market, "futures", risk, lookback_days, timeframe=timeframe)
    if not cfg.place_orders:
        return []
    cfg.allow_shorts = _env_bool("BACKGROUND_ALLOW_SHORTS", False)

    slot = _EXECUTION_STATE.setdefault(pair, {"state": bot.TradeState(), "last_ts": None})
    state = slot["state"]
    last_ts = slot["last_ts"]
    assert isinstance(state, bot.TradeState)

    bars, latest_closed, used_pair, used_source = bot.fetch_closed_bars(
        cfg.pair,
        cfg.market,
        lookback_days=cfg.lookback_days,
        execution_mode=cfg.execution_mode,
        timeframe=cfg.timeframe,
    )
    if bars.empty:
        return [f"{pair}: no bars fetched for background execution."]
    bars = bars[bars.index <= latest_closed]
    frame = bot.build_confluence_frame(bars)
    if frame.empty:
        return [f"{pair}: confluence warmup in progress."]

    events: list[str] = []
    if last_ts is None:
        boot_cfg = bot.RuntimeConfig(**{**cfg.__dict__, "place_orders": False})
        for ts, row in frame.iterrows():
            bot.process_closed_bar(ts, row, state, boot_cfg, frame.loc[:ts])
            slot["last_ts"] = ts
        last_ts = slot["last_ts"]
        if isinstance(last_ts, pd.Timestamp):
            events.append(f"{pair}: background executor bootstrapped to {bot._fmt_ts(last_ts)}.")
            state = bot.TradeState(realized_pnl=state.realized_pnl)
            slot["state"] = state
            for ev in bot.sync_futures_position_state(last_ts, state, cfg):
                events.append(ev)
            last_row = frame.loc[last_ts]
            for ev in bot.process_closed_bar(last_ts, last_row, state, cfg, frame.loc[:last_ts]):
                events.append(ev)
        return events

    assert isinstance(last_ts, pd.Timestamp)
    new_rows = frame[frame.index > last_ts]
    for ts, row in new_rows.iterrows():
        for ev in bot.sync_futures_position_state(ts, state, cfg):
            events.append(ev)
        for ev in bot.process_closed_bar(ts, row, state, cfg, frame.loc[:ts]):
            events.append(ev)
        slot["last_ts"] = ts
    if used_pair or used_source:
        cfg.candle_pair = used_pair
        cfg.data_source = used_source
    return events


async def _execute_saved_tracker_orders(settings: dict, coins: list[str], strategy_id: str, risk: float, lookback_days: int, timeframe: str | None = None) -> None:
    if str(settings.get("executionMode") or "").lower() != "real":
        return
    if strategy_id != "confluence":
        logger.info("Background executor skipped: strategy %s is scanner-only.", strategy_id)
        return
    if not _env_bool("PLACE_ORDERS") and not _env_bool("COINDCX_PLACE_ORDERS") and not _env_bool("BOT_PLACE_ORDERS"):
        return

    max_symbols = max(1, min(int(float(os.getenv("BACKGROUND_EXECUTOR_MAX_SYMBOLS", "3"))), 12))
    for coin in coins[:max_symbols]:
        try:
            events = await asyncio.to_thread(_execute_one, coin, risk, lookback_days, timeframe)
            for event in events:
                logger.info("Background executor: %s", event)
        except Exception as exc:
            logger.warning("Background executor failed for %s: %s", coin, exc)


async def scan_saved_tracker_coins() -> None:
    settings = await _load_dashboard_settings()
    if not settings.get("trackingActive"):
        return

    coins = _normalise_coins(settings.get("selectedCoins"))
    if not coins:
        return

    strategy_id = str(settings.get("strategy") or "confluence")
    try:
        risk = max(0.01, min(float(settings.get("risk") or 10.0), 1_000_000.0))
    except (TypeError, ValueError):
        risk = 10.0
    try:
        lookback_days = max(1, min(int(float(settings.get("lookback") or 2)), 14))
    except (TypeError, ValueError):
        lookback_days = 2
    try:
        timeframe, _, _ = bot._normalize_timeframe(settings.get("timeframe") or None)
    except Exception:
        timeframe, _, _ = bot._normalize_timeframe(None)

    rows: list[dict] = []
    for coin in coins:
        try:
            rows.append(await asyncio.to_thread(_snapshot_one, coin, strategy_id, risk, lookback_days, timeframe))
        except Exception as exc:
            logger.warning("Background tracker failed for %s: %s", coin, exc)

    if rows:
        await store_tracker_signal_events(rows)
        logger.info("Background tracker stored %d signal rows for %s", len(rows), ",".join(coins))

    await _execute_saved_tracker_orders(settings, coins, strategy_id, risk, lookback_days, timeframe)
