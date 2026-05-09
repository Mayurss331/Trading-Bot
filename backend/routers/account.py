from __future__ import annotations

import asyncio

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..bot_loader import bot
from ..db.persistence import store_account_snapshot
from ..utils import clean, coin_from_pair, make_cfg, DEFAULT_PAIR, DEFAULT_MARKET

router = APIRouter(tags=["account"])


@router.get("/api/account")
async def account(
    pair: str = DEFAULT_PAIR,
    market: str = "",
    mode: str = "futures",
) -> JSONResponse:
    if not market:
        market = bot.derive_market_from_pair(pair) or DEFAULT_MARKET
    mode = mode.lower() if mode.lower() in {"spot", "margin", "futures"} else "futures"
    cfg = make_cfg(pair, market, mode, 10.0, 1)

    if not (cfg.api_key and cfg.api_secret):
        return JSONResponse({
            "ok": True,
            "has_credentials": False,
            "mode": mode,
            "message": "COINDCX_API_KEY/SECRET are not set.",
            "risk_suggestions": [],
        })

    def _sync_account() -> dict:
        available, currency = bot.fetch_available_quote_balance(cfg)
        available = float(available or 0.0)
        suggestions = [
            {"label": "0.25%", "percent": 0.25, "amount": available * 0.0025},
            {"label": "0.5%", "percent": 0.5, "amount": available * 0.005},
            {"label": "1%", "percent": 1.0, "amount": available * 0.01},
            {"label": "2%", "percent": 2.0, "amount": available * 0.02},
        ]
        wallets: list = []
        positions: list = []
        if mode == "futures":
            try:
                wallets = bot.fetch_futures_wallets(cfg)
            except Exception:
                wallets = []
            try:
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
                if isinstance(data, list):
                    for row in data:
                        active_pos = float(row.get("active_pos") or 0.0)
                        if abs(active_pos) <= 1e-12:
                            continue
                        pair_value = str(row.get("pair") or "")
                        positions.append({
                            "id": row.get("id"),
                            "pair": pair_value,
                            "coin": coin_from_pair(pair_value),
                            "side": "LONG" if active_pos > 0 else "SHORT",
                            "active_pos": active_pos,
                            "quantity": abs(active_pos),
                            "avg_price": row.get("avg_price"),
                            "mark_price": row.get("mark_price") or row.get("last_price"),
                            "liquidation_price": row.get("liquidation_price"),
                            "take_profit_trigger": row.get("take_profit_trigger"),
                            "stop_loss_trigger": row.get("stop_loss_trigger"),
                            "unrealized_pnl": row.get("unrealized_pnl") or row.get("pnl"),
                            "margin": row.get("margin"),
                            "leverage": row.get("leverage"),
                        })
            except Exception:
                positions = []
        return {
            "ok": True,
            "has_credentials": True,
            "mode": mode,
            "currency": currency,
            "available_quote_balance": available,
            "risk_suggestions": suggestions,
            "wallets": wallets,
            "positions": positions,
        }

    result = clean(await asyncio.to_thread(_sync_account))
    await store_account_snapshot(result)
    return JSONResponse(result)
