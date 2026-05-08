"""APScheduler job: fetch 5-minute candles and persist them to SQLite."""
from __future__ import annotations

import asyncio
import logging

import pandas as pd
from sqlalchemy import insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..bot_loader import bot
from ..db.database import AsyncSessionLocal
from ..db.models import Candle

logger = logging.getLogger(__name__)

TRACKED_PAIRS = [
    "B-BTC_USDT", "B-ETH_USDT", "B-SOL_USDT", "B-XRP_USDT",
    "B-BNB_USDT", "B-DOGE_USDT", "B-ADA_USDT", "B-AVAX_USDT",
]


def _fetch_bars_sync(pair: str) -> pd.DataFrame:
    try:
        bars, *_ = bot.fetch_closed_bars(pair, pair.replace("B-", "").replace("_USDT", "USDT"),
                                         lookback_days=1, execution_mode="futures")
        return bars
    except Exception as exc:
        logger.debug("Candle fetch failed for %s: %s", pair, exc)
        return pd.DataFrame()


async def aggregate_candles() -> None:
    for pair in TRACKED_PAIRS:
        try:
            bars = await asyncio.to_thread(_fetch_bars_sync, pair)
            if bars.empty:
                continue
            rows = []
            for ts, row in bars.iterrows():
                rows.append({
                    "pair": pair,
                    "ts": ts.to_pydatetime().replace(tzinfo=None),
                    "open": float(row.get("Open") or 0),
                    "high": float(row.get("High") or 0),
                    "low": float(row.get("Low") or 0),
                    "close": float(row.get("Close") or 0),
                    "volume": float(row.get("Volume") or 0),
                })
            if not rows:
                continue
            async with AsyncSessionLocal() as db:
                stmt = (
                    sqlite_insert(Candle)
                    .values(rows)
                    .on_conflict_do_nothing(index_elements=["pair", "ts"])
                )
                await db.execute(stmt)
                await db.commit()
            logger.debug("Stored %d candles for %s", len(rows), pair)
        except Exception as exc:
            logger.warning("Candle store error for %s: %s", pair, exc)
