from __future__ import annotations

import time

import httpx

BINANCE_BASE = "https://web3.binance.com/bapi/defi/v1/public/wallet-direct"
_HEADERS = {
    "Accept-Encoding": "identity",
    "Content-Type": "application/json",
    "User-Agent": "binance-web3/2.1 (Skill)",
}
_TTL = 60.0
_cache: dict[str, tuple[float, object]] = {}


def _cache_get(key: str) -> object | None:
    entry = _cache.get(key)
    if entry and time.monotonic() - entry[0] < _TTL:
        return entry[1]
    return None


def _cache_set(key: str, value: object) -> object:
    _cache[key] = (time.monotonic(), value)
    return value


async def get_smart_money_signals(chain: str = "CT_501", page_size: int = 50) -> list[dict]:
    key = f"signals:{chain}"
    if (hit := _cache_get(key)) is not None:
        return hit  # type: ignore[return-value]
    url = f"{BINANCE_BASE}/buw/wallet/web/signal/smart-money/ai"
    payload = {"smartSignalType": "", "page": 1, "pageSize": page_size, "chainId": chain}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=payload, headers=_HEADERS)
            r.raise_for_status()
            data = r.json().get("data") or []
    except Exception:
        data = []
    return _cache_set(key, data)  # type: ignore[return-value]


async def get_smart_money_inflow_rank(chain: str = "CT_501", period: str = "24h") -> list[dict]:
    key = f"inflow:{chain}:{period}"
    if (hit := _cache_get(key)) is not None:
        return hit  # type: ignore[return-value]
    url = f"{BINANCE_BASE}/tracker/wallet/token/inflow/rank/query/ai"
    payload = {"chainId": chain, "period": period, "tagType": 2}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=payload, headers=_HEADERS)
            r.raise_for_status()
            data = r.json().get("data") or []
    except Exception:
        data = []
    return _cache_set(key, data)  # type: ignore[return-value]


async def get_trending_tokens(chain: str = "CT_501", period: int = 50) -> list[dict]:
    key = f"trending:{chain}:{period}"
    if (hit := _cache_get(key)) is not None:
        return hit  # type: ignore[return-value]
    url = f"{BINANCE_BASE}/buw/wallet/market/token/pulse/unified/rank/list/ai"
    payload = {"rankType": 10, "chainId": chain, "period": period, "size": 20}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=payload, headers=_HEADERS)
            r.raise_for_status()
            data = r.json().get("data", {}).get("tokens") or []
    except Exception:
        data = []
    return _cache_set(key, data)  # type: ignore[return-value]


async def get_social_hype(chain: str = "CT_501") -> list[dict]:
    key = f"social:{chain}"
    if (hit := _cache_get(key)) is not None:
        return hit  # type: ignore[return-value]
    url = f"{BINANCE_BASE}/buw/wallet/market/token/pulse/social/hype/rank/leaderboard/ai"
    params = {
        "chainId": chain,
        "sentiment": "All",
        "socialLanguage": "ALL",
        "targetLanguage": "en",
        "timeRange": 1,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, params=params, headers=_HEADERS)
            r.raise_for_status()
            data = r.json().get("data", {}).get("leaderBoardList") or []
    except Exception:
        data = []
    return _cache_set(key, data)  # type: ignore[return-value]
