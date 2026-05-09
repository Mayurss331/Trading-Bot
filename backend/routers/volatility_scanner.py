"""Volatility Scanner — find high-volatility futures coins priced ≤ $10."""
from __future__ import annotations

import asyncio
import time

import numpy as np
import pandas as pd
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..bot_loader import bot
from ..utils import clean, coin_from_pair, futures_pair_for_coin, futures_market_for_coin


router = APIRouter(tags=["volatility_scanner"])

# Re-use the catalog cache from snapshot router
from .snapshot import _sync_fetch_catalog  # noqa: E402

MAX_PRICE_USD = 10.0


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Average True Range."""
    tr = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - df["Close"].shift(1)).abs(),
            (df["Low"] - df["Close"].shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(span=n, adjust=False).mean()


def _compute_volatility(bars: pd.DataFrame) -> dict | None:
    """Compute volatility metrics from OHLCV bars. Returns None if insufficient data."""
    if bars.empty or len(bars) < 20:
        return None

    close = bars["Close"]
    high = bars["High"]
    low = bars["Low"]
    volume = bars["Volume"]
    last_close = float(close.iloc[-1])

    if last_close <= 0 or not np.isfinite(last_close):
        return None

    # Filter: only coins priced ≤ $10
    if last_close > MAX_PRICE_USD:
        return None

    # ATR % (14-period ATR as % of current price)
    atr_series = _atr(bars, 14)
    atr_val = float(atr_series.iloc[-1]) if not atr_series.empty else 0.0
    atr_pct = (atr_val / last_close) * 100 if last_close > 0 else 0.0

    # Range % (full period high-low as % of price)
    period_high = float(high.max())
    period_low = float(low.min())
    range_pct = ((period_high - period_low) / last_close) * 100 if last_close > 0 else 0.0

    # StdDev % (standard deviation of returns)
    returns = close.pct_change().dropna()
    stddev_pct = float(returns.std() * 100) if len(returns) > 1 else 0.0

    # Average volume (USDT terms, last 20 bars)
    recent_vol = volume.tail(20)
    avg_volume_usd = float(recent_vol.mean() * last_close) if not recent_vol.empty else 0.0

    # Price change % over period
    first_close = float(close.iloc[0])
    change_pct = ((last_close / first_close) - 1) * 100 if first_close > 0 else 0.0

    return {
        "price": round(last_close, 8),
        "atr_pct": round(atr_pct, 3),
        "range_pct": round(range_pct, 2),
        "stddev_pct": round(stddev_pct, 4),
        "volume_usd": round(avg_volume_usd, 2),
        "change_pct": round(change_pct, 2),
        "bars_count": len(bars),
    }


def _sync_scan_coin(coin: str) -> dict | None:
    """Fetch bars and compute volatility for a single coin."""
    pair = futures_pair_for_coin(coin)
    market = futures_market_for_coin(coin)
    try:
        bars, _, used_pair, used_source = bot.fetch_closed_bars(
            pair, market,
            lookback_days=2,
            execution_mode="futures",
            timeframe="15m",
        )
    except Exception:
        return None

    if bars.empty:
        return None

    metrics = _compute_volatility(bars)
    if metrics is None:
        return None

    return {
        "coin": coin,
        "pair": pair,
        "market": market,
        **metrics,
    }


@router.get("/api/volatility-scan")
async def volatility_scan(
    max_price: float = MAX_PRICE_USD,
) -> JSONResponse:
    """Scan all futures coins for volatility, filter to price ≤ max_price."""
    start = time.time()

    # Get all available futures coins
    catalog = await asyncio.to_thread(_sync_fetch_catalog)
    coins = [c["coin"] for c in catalog if c.get("coin")]

    # Scan all coins concurrently
    async def _scan(coin: str) -> dict | None:
        return await asyncio.to_thread(_sync_scan_coin, coin)

    results = await asyncio.gather(*[_scan(c) for c in coins], return_exceptions=True)

    # Filter out failures and apply price filter
    rows = []
    for r in results:
        if isinstance(r, dict) and r is not None:
            if r["price"] <= max_price:
                rows.append(r)

    # Sort by ATR% descending (highest volatility first)
    rows.sort(key=lambda x: x.get("atr_pct", 0), reverse=True)

    # Add rank
    for i, row in enumerate(rows, 1):
        row["rank"] = i

    elapsed = round(time.time() - start, 2)

    return JSONResponse(clean({
        "ok": True,
        "count": len(rows),
        "total_scanned": len(coins),
        "max_price": max_price,
        "elapsed_seconds": elapsed,
        "coins": rows,
    }))
