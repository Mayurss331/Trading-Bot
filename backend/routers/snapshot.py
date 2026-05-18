from __future__ import annotations

import asyncio
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import time

import pandas as pd
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import select

from ..bot_loader import bot, ROOT
from ..db.database import AsyncSessionLocal
from ..db.models import CustomStrategy, Trade
from ..db.persistence import store_signal_event, store_tracker_signal_events, store_trade
from ..utils import (
    bars_payload,
    clean,
    coin_from_pair,
    float_param,
    futures_market_for_coin,
    futures_pair_for_coin,
    env_bool,
    int_param,
    make_cfg,
    str_param,
    DEFAULT_PAIR,
    DEFAULT_MARKET,
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default

# Strategies loaded once at module level
sys.path.insert(0, str(ROOT))
from strategies.base import StrategyContext  # noqa: E402
from strategies.registry import get_strategy, list_strategies, normalize_strategy_id  # noqa: E402

router = APIRouter(tags=["snapshot"])

DEFAULT_FUTURES_COINS = [
    "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE",
    "ADA", "LINK", "AVAX", "DOT", "LTC", "TRX",
]

_CATALOG_CACHE: dict[str, object] = {"ts": 0.0, "coins": []}
_CATALOG_TTL = 600
MIN_SNAPSHOT_BARS = 60
MAX_SNAPSHOT_BARS = 10_000


# ---------------------------------------------------------------------------
# Futures catalog
# ---------------------------------------------------------------------------

def _fetch_active_futures_instrument(pair: str) -> dict | None:
    try:
        instrument = bot.fetch_futures_instrument_details(pair, "USDT")
    except Exception:
        return None
    if str(instrument.get("status", "")).lower() != "active":
        return None
    if str(instrument.get("kind", "")).lower() != "perpetual":
        return None
    instrument_pair = str(instrument.get("pair") or pair)
    coin = str(
        instrument.get("underlying_currency_short_name")
        or instrument.get("position_currency_short_name")
        or coin_from_pair(instrument_pair)
    ).upper()
    return {
        "coin": coin,
        "pair": instrument_pair,
        "market": futures_market_for_coin(coin),
        "status": str(instrument.get("status") or "active"),
        "max_leverage": max(
            float(instrument.get("max_leverage_long") or 0),
            float(instrument.get("max_leverage_short") or 0),
        ) or None,
        "min_notional": instrument.get("min_notional"),
        "price_increment": instrument.get("price_increment"),
        "quantity_increment": instrument.get("quantity_increment"),
    }


def _sync_fetch_catalog(force: bool = False) -> list[dict]:
    now = time.time()
    cached = _CATALOG_CACHE.get("coins")
    if (
        not force
        and isinstance(cached, list)
        and cached
        and now - float(_CATALOG_CACHE.get("ts") or 0) < _CATALOG_TTL
    ):
        return cached

    default_pairs = [futures_pair_for_coin(c) for c in DEFAULT_FUTURES_COINS]
    try:
        markets = bot.fetch_markets_details()
    except Exception:
        markets = []

    pairs = default_pairs.copy()
    for row in markets:
        pair = str(row.get("pair") or "")
        status = str(row.get("status") or "").lower()
        if status != "active" or not pair.startswith("B-") or not pair.endswith("_USDT"):
            continue
        if pair not in pairs:
            pairs.append(pair)

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = {executor.submit(_fetch_active_futures_instrument, p): p for p in pairs}
        for future in as_completed(futures):
            row = future.result()
            if row:
                rows.append(row)

    popular_rank = {coin: idx for idx, coin in enumerate(DEFAULT_FUTURES_COINS)}
    rows = sorted(rows, key=lambda r: (popular_rank.get(str(r["coin"]), 999), str(r["coin"])))
    if not rows:
        rows = [
            {"coin": c, "pair": futures_pair_for_coin(c), "market": futures_market_for_coin(c),
             "status": "fallback", "max_leverage": None, "min_notional": None}
            for c in DEFAULT_FUTURES_COINS
        ]

    _CATALOG_CACHE["ts"] = now
    _CATALOG_CACHE["coins"] = rows
    return rows


# ---------------------------------------------------------------------------
# Snapshot builder
# ---------------------------------------------------------------------------

def _sync_build_snapshot(
    pair: str, market: str, strategy_id: str, mode: str,
    lookback_days: int, limit: int, risk: float,
    pair2: str | None = None, market2: str | None = None,
    timeframe: str | None = None,
    exec_mode: str = "",
    custom_analyzer=None,
) -> dict:
    cfg = make_cfg(pair, market, mode, risk, lookback_days, timeframe=timeframe, exec_mode=exec_mode)
    bars, latest_closed, used_pair, used_source = bot.fetch_closed_bars(
        cfg.pair,
        cfg.market,
        lookback_days=cfg.lookback_days,
        limit=limit,
        execution_mode=cfg.execution_mode,
        timeframe=cfg.timeframe,
    )
    if bars.empty:
        return {
            "ok": False,
            "message": "No CoinDCX bars returned for this pair/market.",
            "pair": pair, "market": market, "mode": mode, "bars": [],
        }

    spot_ticker = futures_ticker = None
    try:
        if mode != "futures" or strategy_id == "arbitrage":
            spot_ticker = bot.fetch_market_ticker(market)
    except Exception:
        pass
    try:
        if mode == "futures" or strategy_id == "arbitrage":
            futures_ticker = bot.fetch_futures_ticker(pair)
    except Exception:
        pass

    # Secondary bars for pairs_stat_arb scanner
    pair2_bars = None
    if strategy_id == "pairs_stat_arb" and pair2:
        if not market2:
            market2 = bot.derive_market_from_pair(pair2) or pair2.replace("B-", "").replace("_", "")
        try:
            p2_bars, _, _, _ = bot.fetch_closed_bars(
                pair2, market2, lookback_days=cfg.lookback_days,
                limit=limit,
                execution_mode=cfg.execution_mode,
                timeframe=cfg.timeframe,
            )
            if not p2_bars.empty:
                pair2_bars = p2_bars
        except Exception:
            pass  # pair2_bars stays None; strategy will return WAIT

    ctx = StrategyContext(
        pair=pair, market=market, mode=mode, risk=risk,
        allow_shorts=cfg.allow_shorts,
        extras={
            "spot_ticker": spot_ticker,
            "futures_ticker": futures_ticker,
            "pair2_bars": pair2_bars,
        },
    )
    analyzer = custom_analyzer.analyze if custom_analyzer is not None else get_strategy(strategy_id)
    analysis = analyzer(bars, ctx)
    frame = analysis["frame"]
    meta = analysis["meta"]
    ticker = futures_ticker if mode == "futures" else spot_ticker

    last_bar = bars.iloc[-1]
    first_close = float(bars["Close"].iloc[0])
    last_close = float(last_bar["Close"])
    latest_frame = frame.iloc[-1] if not frame.empty else None
    score = float(latest_frame["score"]) if latest_frame is not None else 0.0
    freshness = bot._freshness_minutes(bars.index.max())

    return {
        "ok": True,
        "coin": coin_from_pair(pair),
        "pair": pair, "market": market, "mode": mode,
        "timeframe": cfg.timeframe,
        "strategy": {
            "id": meta.id, "name": meta.name,
            "description": meta.description,
            "chart_label": meta.chart_label,
            "score_label": meta.score_label,
            "reason": analysis.get("reason"),
            "notes": analysis.get("notes", []),
        },
        "used_pair": used_pair,
        "used_source": used_source,
        "latest_closed": latest_closed,
        "freshness_minutes": freshness,
        "ticker": ticker,
        "stats": {
            "bars": len(bars),
            "latest_time": bars.index.max(),
            "open": last_bar["Open"],
            "high": float(bars["High"].tail(limit).max()),
            "low": float(bars["Low"].tail(limit).min()),
            "close": last_close,
            "change": last_close - first_close,
            "change_pct": ((last_close / first_close) - 1) * 100 if first_close else None,
            "score": score,
            "rsi": None if latest_frame is None else latest_frame.get("rsi"),
            "supertrend": None if latest_frame is None else latest_frame.get("st_line"),
        },
        "indicators": analysis.get("indicators", {}),
        "action": analysis["action"],
        "state": analysis["state"],
        "events": analysis["events"],
        "bars": bars_payload(
            bars, frame, limit,
            extra_cols=(
                ["phase", "fvg_low", "fvg_high", "entry_side", "bias", "bos", "sweep", "choch"]
                if strategy_id == "daily_sweep"
                else ["entry_side", "exit_long", "exit_short"]
            ),
        ),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _snapshot_bar_limit(lookback_days: int, bar_minutes: int, requested_limit: int | None) -> int:
    if requested_limit is None:
        requested_limit = (lookback_days * 24 * 60) // max(1, bar_minutes) + 2
    return max(MIN_SNAPSHOT_BARS, min(int(requested_limit), MAX_SNAPSHOT_BARS))


@router.get("/api/health")
async def health() -> JSONResponse:
    return JSONResponse({
        "ok": True,
        "service": "coindcx-dashboard",
        "place_orders": env_bool("PLACE_ORDERS") or env_bool("COINDCX_PLACE_ORDERS") or env_bool("BOT_PLACE_ORDERS"),
        "background_tracker_enabled": env_bool("BACKGROUND_TRACKER_ENABLED", True),
        "background_tracker_interval_seconds": max(10, _env_int("BACKGROUND_TRACKER_INTERVAL_SECONDS", 30)),
    })


async def _load_custom_analyzer_for_snapshot(custom_strategy_id: int | None):
    if not custom_strategy_id:
        return None
    from ..backtesting.strategy_loader import compile_custom_strategy
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(CustomStrategy).where(
                    CustomStrategy.id == custom_strategy_id,
                    CustomStrategy.enabled.is_(True),
                )
            )
            row = result.scalar_one_or_none()
        if row is None:
            return None
        return compile_custom_strategy(row)
    except Exception:
        return None


@router.get("/api/snapshot")
async def snapshot(
    pair: str = DEFAULT_PAIR,
    market: str = "",
    strategy: str = "confluence",
    mode: str = "futures",
    lookback_days: int = 3,
    limit: int | None = None,
    risk: float = 10.0,
    pair2: str = "",
    market2: str = "",
    timeframe: str = "",
    exec_mode: str = "",
    custom_strategy_id: int | None = None,
) -> JSONResponse:
    if not market:
        market = bot.derive_market_from_pair(pair) or DEFAULT_MARKET
    mode = mode.lower() if mode.lower() in {"spot", "margin", "futures"} else "spot"
    lookback_days = max(1, min(lookback_days, 30))
    risk = max(0.01, min(risk, 1_000_000.0))
    tf, _, bar_minutes = bot._normalize_timeframe(timeframe or None)
    limit = _snapshot_bar_limit(lookback_days, bar_minutes, limit)
    exec_mode = exec_mode.lower() if exec_mode.lower() in {"paper", "real"} else ""

    custom_analyzer = await _load_custom_analyzer_for_snapshot(custom_strategy_id)
    if custom_analyzer is not None:
        strategy_id = custom_analyzer.id
    else:
        strategy_id = normalize_strategy_id(strategy)

    result = await asyncio.to_thread(
        _sync_build_snapshot, pair, market, strategy_id, mode, lookback_days, limit, risk,
        pair2=pair2 or None, market2=market2 or None,
        timeframe=tf, exec_mode=exec_mode,
        custom_analyzer=custom_analyzer,
    )
    payload = clean(result)
    if isinstance(payload, dict):
        payload["timeframe"] = tf
    await store_signal_event(payload)
    for trade in bot.drain_completed_trades():
        await store_trade(trade)
    return JSONResponse(payload)


@router.get("/api/strategies")
async def strategies() -> JSONResponse:
    return JSONResponse({"ok": True, "strategies": list_strategies()})


@router.get("/api/timeframes")
async def timeframes() -> JSONResponse:
    return JSONResponse({"ok": True, "timeframes": bot.list_timeframes()})


@router.get("/api/futures-markets")
async def futures_markets(force: bool = False) -> JSONResponse:
    coins = await asyncio.to_thread(_sync_fetch_catalog, force)
    source = "coindcx_futures_instruments" if coins and coins[0].get("status") != "fallback" else "fallback"
    return JSONResponse({"ok": True, "source": source, "count": len(coins), "coins": coins})


@router.get("/api/markets")
async def markets(q: str = "", limit: int = 80) -> JSONResponse:
    limit = max(1, min(limit, 300))
    search = q.upper()

    def _sync() -> list[dict]:
        market_list = bot.fetch_markets_details()
        rows: list[dict] = []
        for row in market_list:
            pair = str(row.get("pair") or "")
            symbol = str(row.get("symbol") or "")
            if search and search not in f"{pair} {symbol}".upper():
                continue
            rows.append({
                "pair": pair,
                "market": symbol,
                "ecode": row.get("ecode"),
                "status": row.get("status"),
                "target_currency": row.get("target_currency_short_name"),
                "base_currency": row.get("base_currency_short_name"),
            })
            if len(rows) >= limit:
                break
        return rows

    rows = await asyncio.to_thread(_sync)
    return JSONResponse({"ok": True, "markets": rows})


@router.get("/api/track")
async def track(
    coins: str = "BTC,ETH,SOL",
    strategy: str = "confluence",
    risk: float = 10.0,
    lookback_days: int = 2,
    timeframe: str = "",
    exec_mode: str = "",
) -> JSONResponse:
    strategy_id = normalize_strategy_id(strategy)
    risk = max(0.01, min(risk, 1_000_000.0))
    lookback_days = max(1, min(lookback_days, 14))
    tf, _, _ = bot._normalize_timeframe(timeframe or None)
    exec_mode = exec_mode.lower() if exec_mode.lower() in {"paper", "real"} else ""

    coin_list = []
    for item in coins.replace(" ", "").split(","):
        if not item:
            continue
        coin = coin_from_pair(item) if ("_" in item or "-" in item) else item.upper()
        if coin not in coin_list:
            coin_list.append(coin)
    coin_list = coin_list[:12]

    async def _fetch_one(coin: str) -> dict:
        pair = futures_pair_for_coin(coin)
        market = futures_market_for_coin(coin)
        try:
            snap = await asyncio.to_thread(
                _sync_build_snapshot,
                pair,
                market,
                strategy_id,
                "futures",
                lookback_days,
                80,
                risk,
                timeframe=tf,
                exec_mode=exec_mode,
            )
        except Exception as exc:
            return {"ok": False, "coin": coin, "pair": pair, "market": market, "message": str(exc)}

        strategy_info = snap.get("strategy") or {}
        stats = snap.get("stats") or {}
        action = snap.get("action") or {}
        state = snap.get("state") or {}
        ticker = snap.get("ticker") or {}
        return {
            "ok": bool(snap.get("ok")),
            "coin": snap.get("coin") or coin,
            "pair": snap.get("pair"),
            "market": snap.get("market"),
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
            "message": snap.get("message"),
        }

    rows = await asyncio.gather(*[_fetch_one(c) for c in coin_list], return_exceptions=False)
    clean_rows = clean(list(rows))
    await store_tracker_signal_events(clean_rows)
    for trade in bot.drain_completed_trades():
        await store_trade(trade)
    return JSONResponse(clean({
        "ok": True,
        "mode": "futures",
        "strategy": strategy_id,
        "tracked": clean_rows,
        "server_time": pd.Timestamp.now(tz=bot.IST),
    }))


@router.get("/api/trades")
async def get_trades(limit: int = 50) -> JSONResponse:
    limit = max(1, min(limit, 200))
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Trade).order_by(Trade.entry_ts.desc()).limit(limit)
        )
        trades = result.scalars().all()
    rows = [
        {
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
        }
        for t in trades
    ]
    return JSONResponse({"ok": True, "count": len(rows), "trades": rows})
