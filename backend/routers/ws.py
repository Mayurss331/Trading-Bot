from __future__ import annotations

import asyncio

import pandas as pd
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..bot_loader import bot
from ..utils import clean, coin_from_pair, DEFAULT_PAIR

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/quotes")
async def quote_stream(
    ws: WebSocket,
    pair: str = DEFAULT_PAIR,
    market: str = "",
    mode: str = "futures",
    interval: int = 3,
) -> None:
    await ws.accept()
    interval = max(1, min(interval, 10))
    if not market:
        market = bot.derive_market_from_pair(pair) or "ETHUSDT"

    try:
        while True:
            try:
                if mode == "futures":
                    ticker = await asyncio.to_thread(bot.fetch_futures_ticker, pair)
                else:
                    ticker = await asyncio.to_thread(bot.fetch_market_ticker, market)
            except Exception:
                ticker = None

            last_price = None
            bid = None
            ask = None
            if ticker:
                for key in ("last_price", "last"):
                    try:
                        last_price = float(ticker.get(key))
                        break
                    except Exception:
                        pass
                try:
                    bid = float(ticker.get("bid"))
                except Exception:
                    pass
                try:
                    ask = float(ticker.get("ask"))
                except Exception:
                    pass

            payload = clean({
                "ok": bool(ticker),
                "pair": pair,
                "market": market,
                "mode": mode,
                "coin": coin_from_pair(pair),
                "server_time": pd.Timestamp.now(tz=bot.IST),
                "last_price": last_price,
                "bid": bid,
                "ask": ask,
            })
            await ws.send_json(payload)
            await asyncio.sleep(interval)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
