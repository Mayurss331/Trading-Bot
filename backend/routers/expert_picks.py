"""Expert Picks — Smart signals and recommendations for your portfolio."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from ..bot_loader import bot
from ..utils import clean, coin_from_pair, make_cfg

logger = logging.getLogger(__name__)
router = APIRouter(tags=["expert_picks"])

MYCRYPTOSIGNAL_API_KEY = os.getenv(
    "MYCRYPTOSIGNAL_API_KEY",
    "mcs_bf873fd7c49132788be6b243381c366be07ae7b5cf3c14c16b4019357fa2ab29"
)
MYCRYPTOSIGNAL_URL = "https://mycryptosignal.axiopistis-systems.workers.dev/api/signals"
ALTERNATIVE_ME_URL = "https://api.alternative.me/fng/"

CACHE_TTL_SECONDS = 300

_cached_signals: dict[str, Any] | None = None
_cache_timestamp: float = 0


async def _fetch_with_retry(url: str, headers: dict | None = None, timeout: int = 10) -> dict | None:
    """Fetch URL with retry logic."""
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    return resp.json()
                logger.warning(f"HTTP {resp.status_code} from {url}")
        except httpx.TimeoutException:
            logger.warning(f"Timeout fetching {url}, attempt {attempt + 1}")
        except Exception as e:
            logger.warning(f"Error fetching {url}: {e}")
        await asyncio.sleep(1)
    return None


async def _get_signals(force_refresh: bool = False) -> dict:
    """Fetch signals from MyCryptoSignal with 5-minute cache."""
    global _cached_signals, _cache_timestamp

    now = time.time()
    if not force_refresh and _cached_signals is not None and (now - _cache_timestamp) < CACHE_TTL_SECONDS:
        logger.debug("Returning cached signals")
        return _cached_signals

    headers = {"X-API-Key": MYCRYPTOSIGNAL_API_KEY}
    data = await _fetch_with_retry(MYCRYPTOSIGNAL_URL, headers=headers)

    if not data or "signals" not in data:
        logger.warning("MyCryptoSignal API returned no signals, using cached if available")
        return _cached_signals or {"signals": [], "error": "No data from API"}

    _cached_signals = data
    _cache_timestamp = now
    logger.info(f"Fetched {len(data.get('signals', []))} signals from MyCryptoSignal")
    return _cached_signals


async def _get_fear_greed() -> dict:
    """Fetch Fear & Greed Index from Alternative.me."""
    data = await _fetch_with_retry(ALTERNATIVE_ME_URL)
    if not data:
        return {"value": None, "classification": "Unknown", "timestamp": None}

    return {
        "value": int(data.get("data", [{}])[0].get("value", 0)),
        "classification": data.get("data", [{}])[0].get("value_classification", "Unknown"),
        "timestamp": data.get("data", [{}])[0].get("timestamp"),
    }


def _normalize_symbol(symbol: str) -> str:
    """Normalize coin symbol for matching (e.g., 'BTC' -> 'BTC', 'btc' -> 'BTC')."""
    return symbol.upper().strip().replace("-", "").replace("_", "")


def _find_signal_for_coin(coin: str, signals: list) -> dict | None:
    """Find signal for a specific coin."""
    normalized_coin = _normalize_symbol(coin)
    for sig in signals:
        sig_symbol = _normalize_symbol(sig.get("symbol", sig.get("coin_id", "")))
        if sig_symbol == normalized_coin:
            return sig
    return None


async def _get_active_positions(cfg) -> list[dict]:
    """Fetch active futures positions from CoinDCX (read-only)."""
    try:
        logger.info(f"Fetching positions with cfg.api_key={bool(cfg.api_key)}, futures_margin_currency={cfg.futures_margin_currency}")
        data = bot._private_post(
            "/exchange/v1/derivatives/futures/positions",
            {
                "timestamp": bot._now_ms(),
                "page": "1",
                "size": "100",
                "margin_currency_short_name": [cfg.futures_margin_currency],
            },
            cfg,
        )
        logger.info(f"Positions response type: {type(data)}, is list: {isinstance(data, list)}")
        if isinstance(data, list):
            positions = []
            for row in data:
                active_pos = float(row.get("active_pos") or 0.0)
                if abs(active_pos) > 1e-12:
                    pair_value = str(row.get("pair") or "")
                    positions.append({
                        "pair": pair_value,
                        "coin": coin_from_pair(pair_value),
                        "side": "LONG" if active_pos > 0 else "SHORT",
                        "quantity": abs(active_pos),
                        "entry_price": row.get("avg_price"),
                        "mark_price": row.get("mark_price") or row.get("last_price"),
                    })
            logger.info(f"Found {len(positions)} active positions")
            return positions
    except Exception as e:
        logger.warning(f"Failed to fetch positions: {e}")
    return []


def _get_recommendation(position_side: str, signal: str) -> tuple[str, str]:
    """Determine recommendation based on position vs signal."""
    if position_side == "LONG":
        if signal in ("SELL", "RISK"):
            return "CLOSE_LONG", "Signal turned bearish while you hold LONG"
        elif signal == "BUY":
            return "HOLD", "Signal confirms your LONG position"
        else:
            return "HOLD", "No clear signal change"

    elif position_side == "SHORT":
        if signal == "BUY":
            return "CLOSE_SHORT", "Signal turned bullish while you hold SHORT"
        elif signal in ("SELL", "RISK"):
            return "HOLD", "Signal confirms your SHORT position"
        else:
            return "HOLD", "No clear signal change"

    return "WATCH", "No position to evaluate"


@router.get("/api/expert-picks/signals")
async def expert_picks_signals(force: bool = Query(False, description="Force refresh cache")) -> JSONResponse:
    """Get raw signals from MyCryptoSignal."""
    signals_data = await _get_signals(force_refresh=force)
    return JSONResponse(clean({
        "ok": True,
        "signals": signals_data.get("signals", []),
        "count": len(signals_data.get("signals", [])),
        "cached": (time.time() - _cache_timestamp) < CACHE_TTL_SECONDS if _cache_timestamp else False,
    }))


@router.get("/api/expert-picks/sentiment")
async def expert_picks_sentiment() -> JSONResponse:
    """Get Fear & Greed Index."""
    sentiment = await _get_fear_greed()
    return JSONResponse(clean({
        "ok": True,
        "value": sentiment.get("value"),
        "classification": sentiment.get("classification"),
        "timestamp": sentiment.get("timestamp"),
    }))


@router.get("/api/expert-picks/recommendations")
async def expert_picks_recommendations() -> JSONResponse:
    """Get cross-referenced recommendations based on active positions + signals."""
    signals_data = await _get_signals()
    signals = signals_data.get("signals", [])

    sentiment = await _get_fear_greed()

    cfg = make_cfg("B-BTC_USDT", "BTCUSDT", "futures", 10.0, 1)
    positions = []
    if cfg.api_key and cfg.api_secret:
        positions = await _get_active_positions(cfg)

    position_recommendations = []
    seen_coins = set()

    for pos in positions:
        coin = pos.get("coin", "")
        if not coin:
            continue
        seen_coins.add(coin.upper())
        signal = _find_signal_for_coin(coin, signals)
        if signal:
            rec, reason = _get_recommendation(pos["side"], signal.get("action", "HOLD"))
            position_recommendations.append({
                "coin": coin,
                "pair": pos.get("pair"),
                "position": pos["side"],
                "quantity": pos["quantity"],
                "entry_price": pos.get("entry_price"),
                "signal": signal.get("action", "HOLD"),
                "confidence": signal.get("confidence"),
                "recommendation": rec,
                "reason": reason,
            })

    top_signals = []
    for sig in signals[:15]:
        coin = sig.get("symbol", sig.get("coin_id", ""))
        if coin.upper() not in seen_coins:
            top_signals.append({
                "coin": coin,
                "action": sig.get("action", "HOLD"),
                "confidence": sig.get("confidence"),
                "name": sig.get("name", coin),
            })

    return JSONResponse(clean({
        "ok": True,
        "sentiment": {
            "value": sentiment.get("value"),
            "classification": sentiment.get("classification"),
            "timestamp": sentiment.get("timestamp"),
        },
        "positions_recommendations": position_recommendations,
        "top_signals": top_signals,
        "attribution": "Powered by MyCryptoSignal",
    }))
