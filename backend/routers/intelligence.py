from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..intelligence import (
    get_smart_money_inflow_rank,
    get_smart_money_signals,
    get_social_hype,
    get_trending_tokens,
)

router = APIRouter(tags=["intelligence"])


@router.get("/api/intelligence/signals")
async def intelligence_signals(chain: str = "CT_501", page_size: int = 50) -> JSONResponse:
    data = await get_smart_money_signals(chain=chain, page_size=min(page_size, 100))
    slim = [
        {
            "ticker": d.get("ticker"),
            "direction": d.get("direction"),
            "smartMoneyCount": d.get("smartMoneyCount"),
            "alertPrice": d.get("alertPrice"),
            "currentPrice": d.get("currentPrice"),
            "maxGain": d.get("maxGain"),
            "exitRate": d.get("exitRate"),
            "status": d.get("status"),
            "signalTriggerTime": d.get("signalTriggerTime"),
            "launchPlatform": d.get("launchPlatform"),
        }
        for d in data
    ]
    return JSONResponse({"ok": True, "chain": chain, "count": len(slim), "signals": slim})


@router.get("/api/intelligence/rankings")
async def intelligence_rankings(chain: str = "CT_501", period: str = "24h") -> JSONResponse:
    data = await get_smart_money_inflow_rank(chain=chain, period=period)
    slim = [
        {
            "tokenName": d.get("tokenName"),
            "price": d.get("price"),
            "priceChangeRate": d.get("priceChangeRate"),
            "inflow": d.get("inflow"),
            "traders": d.get("traders"),
            "marketCap": d.get("marketCap"),
            "volume": d.get("volume"),
            "ca": d.get("ca"),
        }
        for d in data[:20]
    ]
    return JSONResponse({"ok": True, "chain": chain, "period": period, "count": len(slim), "rankings": slim})


@router.get("/api/intelligence/trending")
async def intelligence_trending(chain: str = "CT_501") -> JSONResponse:
    data = await get_trending_tokens(chain=chain)
    slim = [
        {
            "symbol": d.get("symbol"),
            "price": d.get("price"),
            "percentChange24h": d.get("percentChange24h"),
            "volume24h": d.get("volume24h"),
            "marketCap": d.get("marketCap"),
            "holders": d.get("holders"),
        }
        for d in data
    ]
    return JSONResponse({"ok": True, "chain": chain, "count": len(slim), "tokens": slim})


@router.get("/api/intelligence/social")
async def intelligence_social(chain: str = "CT_501") -> JSONResponse:
    data = await get_social_hype(chain=chain)
    slim = [
        {
            "symbol": (d.get("metaInfo") or {}).get("symbol"),
            "socialHype": (d.get("socialHypeInfo") or {}).get("socialHype"),
            "sentiment": (d.get("socialHypeInfo") or {}).get("sentiment"),
            "summary": (d.get("socialHypeInfo") or {}).get("socialSummaryBriefTranslated")
                or (d.get("socialHypeInfo") or {}).get("socialSummaryBrief"),
            "priceChange": (d.get("marketInfo") or {}).get("priceChange"),
            "marketCap": (d.get("marketInfo") or {}).get("marketCap"),
        }
        for d in data[:20]
    ]
    return JSONResponse({"ok": True, "chain": chain, "count": len(slim), "social": slim})
