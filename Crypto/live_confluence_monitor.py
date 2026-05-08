#!/usr/bin/env python3
"""
Live Confluence Monitor (CoinDCX, IST, 5m)
==========================================
Continuously pulls CoinDCX 5m candles, computes confluence score, and emits
ENTRY / EXIT signals with fixed-dollar risk management.

Data source:
  - Public candles API: GET https://public.coindcx.com/market_data/candles

Optional live order execution (disabled by default):
  - Signed private order API: POST https://api.coindcx.com/exchange/v1/orders/create
  - Auth headers:
      X-AUTH-APIKEY, X-AUTH-SIGNATURE (HMAC-SHA256 over compact JSON payload)

Risk model:
  - Fixed risk dollars per trade (default: $100)
  - Qty = risk_dollars / abs(entry - stop)
  - Target = entry +/- 2R

Usage:
  Paper signals only (recommended first):
    python3 Crypto/live_confluence_monitor.py --pair KC-ETH_USDT --risk 100

  Live spot orders (long entries + long exits):
    export COINDCX_API_KEY='...'
    export COINDCX_API_SECRET='...'
    python3 Crypto/live_confluence_monitor.py --pair KC-ETH_USDT --market ETHUSDT --risk 100 --place-orders
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import List, Tuple

import numpy as np
import pandas as pd
import requests
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")

PUBLIC_BASE = "https://public.coindcx.com"
PRIVATE_BASE = "https://api.coindcx.com"

TIMEFRAME = "5m"
BAR_FREQ = "5min"
BAR_MINUTES = 5
MIN_WARMUP_BARS = 30

LONG_ENTRY_SCORE = 3
LONG_EXIT_SCORE = 2
SHORT_ENTRY_SCORE = -3
SHORT_EXIT_SCORE = -2


@dataclass
class RuntimeConfig:
    pair: str
    market: str
    risk_dollars: float
    poll_seconds: int
    place_orders: bool
    allow_shorts: bool
    execution_mode: str
    leverage: float
    margin_ecode: str
    futures_margin_currency: str
    position_margin_type: str
    qty_precision: int
    lookback_days: int
    candle_pair: str | None = None
    data_source: str | None = None
    api_key: str | None = None
    api_secret: str | None = None


@dataclass
class TradeState:
    side: int = 0  # 0 flat, +1 long, -1 short
    trade_id: int = 0
    entry_ts: pd.Timestamp | None = None
    entry_px: float = np.nan
    stop_px: float = np.nan
    target_px: float = np.nan
    qty: float = np.nan
    init_risk_per_unit: float = np.nan
    realized_pnl: float = 0.0
    broker_order_id: str | None = None
    broker_order_status: str | None = None
    exit_pending: bool = False
    position_id: str | None = None


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def rsi_s(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    g = d.clip(lower=0).ewm(com=n - 1, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(com=n - 1, adjust=False).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))


def atr_s(df: pd.DataFrame, n: int = 14) -> pd.Series:
    tr = pd.concat(
        [
            (df["High"] - df["Low"]),
            (df["High"] - df["Close"].shift(1)).abs(),
            (df["Low"] - df["Close"].shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(span=n, adjust=False).mean()


def supertrend(df: pd.DataFrame, n: int = 10, mult: float = 3.5) -> Tuple[pd.Series, pd.Series]:
    mid = (df["High"] + df["Low"]) / 2
    _atr = atr_s(df, n)
    bu_b = mid + mult * _atr
    bl_b = mid - mult * _atr
    bu = bu_b.copy()
    bl = bl_b.copy()
    st = pd.Series(np.nan, index=df.index)

    for i in range(1, len(df)):
        bu.iloc[i] = (
            bu_b.iloc[i]
            if bu_b.iloc[i] < bu.iloc[i - 1] or df["Close"].iloc[i - 1] > bu.iloc[i - 1]
            else bu.iloc[i - 1]
        )
        bl.iloc[i] = (
            bl_b.iloc[i]
            if bl_b.iloc[i] > bl.iloc[i - 1] or df["Close"].iloc[i - 1] < bl.iloc[i - 1]
            else bl.iloc[i - 1]
        )
        prev = st.iloc[i - 1]
        if pd.isna(prev):
            st.iloc[i] = bl.iloc[i]
        elif prev == bu.iloc[i - 1]:
            st.iloc[i] = bl.iloc[i] if df["Close"].iloc[i] > bu.iloc[i] else bu.iloc[i]
        else:
            st.iloc[i] = bu.iloc[i] if df["Close"].iloc[i] < bl.iloc[i] else bl.iloc[i]
    return (df["Close"] > st).astype(int), st


def macd_signals(df: pd.DataFrame) -> Tuple[pd.Series, pd.Series, pd.Series]:
    line = ema(df["Close"], 12) - ema(df["Close"], 26)
    signal = ema(line, 9)
    hist = line - signal
    return line, signal, hist


def bollinger(df: pd.DataFrame, n: int = 20, k: float = 2.0) -> Tuple[pd.Series, pd.Series, pd.Series]:
    mid = sma(df["Close"], n)
    std = df["Close"].rolling(n).std()
    return mid - k * std, mid, mid + k * std


def build_confluence_frame(df: pd.DataFrame) -> pd.DataFrame:
    st_sig, st_line = supertrend(df)
    v_st = st_sig.map({1: 1, 0: -1})

    fast = ema(df["Close"], 9)
    slow = ema(df["Close"], 21)
    rsi = rsi_s(df["Close"], 14)
    v_er = pd.Series(0, index=df.index)
    v_er[(fast > slow) & (rsi > 45)] = 1
    v_er[(fast < slow) & (rsi < 55)] = -1

    _, _, hist = macd_signals(df)
    v_mc = pd.Series(0, index=df.index)
    v_mc[hist > 0] = 1
    v_mc[hist < 0] = -1

    lo, mid, hi = bollinger(df)
    v_bb = pd.Series(0, index=df.index)
    v_bb[df["Close"] > mid] = 1
    v_bb[df["Close"] < mid] = -1
    v_bb[df["Close"] > hi] = 1
    v_bb[df["Close"] < lo] = -1

    v_rs = pd.Series(0, index=df.index)
    v_rs[(rsi > 50) & (rsi < 70)] = 1
    v_rs[(rsi < 50) & (rsi > 30)] = -1
    v_rs[rsi >= 70] = 0
    v_rs[rsi <= 30] = 0

    out = df.copy()
    out["st_line"] = st_line
    out["score"] = v_st + v_er + v_mc + v_bb + v_rs
    out["rsi"] = rsi
    out = out.dropna(subset=["st_line", "score", "rsi"])
    return out


def derive_market_from_pair(pair: str) -> str:
    # Example: B-ETH_USDT -> ETHUSDT
    if "-" in pair:
        pair = pair.split("-", 1)[1]
    return pair.replace("_", "")


def derive_suffix_from_market(market: str) -> str:
    if market.endswith("USDT"):
        return f"{market[:-4]}_USDT"
    if market.endswith("INR"):
        return f"{market[:-3]}_INR"
    return market


def fetch_markets_details() -> List[dict]:
    url = f"{PRIVATE_BASE}/exchange/v1/markets_details"
    res = requests.get(url, timeout=20)
    res.raise_for_status()
    data = res.json()
    if not isinstance(data, list):
        raise ValueError("markets_details response is not a list")
    return data


def resolve_pair(input_pair: str, market: str) -> Tuple[str, str]:
    """
    Resolve pair/market against live CoinDCX metadata.
    Returns (resolved_pair, resolved_market).
    """
    try:
        markets = fetch_markets_details()
    except Exception:
        # If metadata call fails, keep user input and continue.
        return input_pair, market

    active = [m for m in markets if str(m.get("status", "")).lower() == "active"]
    pair_map = {m.get("pair"): m for m in active}
    symbol_map = {}
    for m in active:
        sym = m.get("symbol")
        if sym:
            symbol_map.setdefault(sym, []).append(m)

    if input_pair in pair_map:
        m = pair_map[input_pair]
        return str(m.get("pair")), str(m.get("symbol", market))

    # If pair alias is wrong (e.g. B-ETH_USDT), resolve by symbol.
    cands = symbol_map.get(market, [])
    if not cands:
        return input_pair, market

    # Prefer common spot ecodes first.
    pref = {"KC": 0, "I": 1, "B": 2, "HB": 3}
    cands = sorted(cands, key=lambda m: pref.get(str(m.get("ecode", "")), 99))
    best = cands[0]
    return str(best.get("pair", input_pair)), str(best.get("symbol", market))


def candidate_candle_pairs(pair: str, market: str) -> List[str]:
    if pair:
        return [pair]

    suffix = derive_suffix_from_market(market)
    candidates: List[str] = []

    if suffix.endswith("_USDT"):
        candidates.extend([f"B-{suffix}", f"KC-{suffix}"])
    elif suffix.endswith("_INR"):
        candidates.extend([f"I-{suffix}", f"B-{suffix}", f"KC-{suffix}"])

    # De-duplicate while preserving order.
    seen = set()
    ordered: List[str] = []
    for item in candidates:
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _pair_suffix(pair: str) -> str:
    if "-" in pair:
        return pair.split("-", 1)[1]
    return pair


def _infer_time_unit(ts_values: pd.Series) -> str:
    # CoinDCX docs show ms, but keep this robust in case format changes.
    sample = pd.to_numeric(ts_values, errors="coerce").dropna()
    if sample.empty:
        return "ms"
    med = float(sample.median())
    if med > 1e14:
        return "us"
    if med > 1e11:
        return "ms"
    return "s"


def _futures_resolution(tf: str) -> str:
    if tf.endswith("m"):
        return tf[:-1]
    if tf.endswith("h"):
        return str(int(tf[:-1]) * 60)
    if tf.endswith("d"):
        return str(int(tf[:-1]) * 1440)
    raise ValueError(f"Unsupported futures timeframe: {tf}")


def _fetch_pair_candles(pair: str, lookback_days: int, limit: int = 1000) -> Tuple[pd.DataFrame, pd.Timestamp]:
    url = f"{PUBLIC_BASE}/market_data/candles"
    now_utc = pd.Timestamp.now(tz="UTC")
    end_ms = int(now_utc.timestamp() * 1000)
    start_ms = int((now_utc - pd.Timedelta(days=max(1, lookback_days))).timestamp() * 1000)
    params = {
        "pair": pair,
        "interval": TIMEFRAME,
        "limit": min(limit, 1000),
        "startTime": start_ms,
        "endTime": end_ms,
    }
    res = requests.get(url, params=params, timeout=20)
    res.raise_for_status()
    candles = res.json()

    now_ist = pd.Timestamp.now(tz=IST)
    latest_closed_open = now_ist.floor(BAR_FREQ) - pd.Timedelta(minutes=BAR_MINUTES)

    if not candles:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"]), latest_closed_open

    df = pd.DataFrame(candles)
    expected = {"open", "high", "low", "close", "volume", "time"}
    missing = expected - set(df.columns)
    if missing:
        raise ValueError(f"Candle response missing keys: {sorted(missing)}")

    time_unit = _infer_time_unit(df["time"])
    df["time"] = pd.to_datetime(df["time"], unit=time_unit, utc=True).dt.tz_convert(IST)
    df["open"] = pd.to_numeric(df["open"], errors="coerce")
    df["high"] = pd.to_numeric(df["high"], errors="coerce")
    df["low"] = pd.to_numeric(df["low"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close", "volume", "time"])
    df = df.sort_values("time").drop_duplicates(subset=["time"], keep="last")

    df = df.rename(
        columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        }
    ).set_index("time")
    df = df[df.index <= latest_closed_open]
    return df, latest_closed_open


def _fetch_futures_candles(pair: str, lookback_days: int) -> Tuple[pd.DataFrame, pd.Timestamp]:
    url = f"{PUBLIC_BASE}/market_data/candlesticks"
    now_utc = pd.Timestamp.now(tz="UTC")
    now_sec = int(now_utc.timestamp())
    start_sec = int((now_utc - pd.Timedelta(days=max(1, lookback_days))).timestamp())
    params = {
        "pair": pair,
        "from": start_sec,
        "to": now_sec,
        "resolution": _futures_resolution(TIMEFRAME),
        "pcode": "f",
    }
    res = requests.get(url, params=params, timeout=20)
    res.raise_for_status()
    raw = res.json()

    now_ist = pd.Timestamp.now(tz=IST)
    latest_closed_open = now_ist.floor(BAR_FREQ) - pd.Timedelta(minutes=BAR_MINUTES)
    if isinstance(raw, dict):
        candles = raw.get("data", [])
    elif isinstance(raw, list):
        candles = raw
    else:
        candles = []

    if not candles:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"]), latest_closed_open

    df = pd.DataFrame(candles)
    if df.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"]), latest_closed_open
    time_col = None
    for candidate in ("time", "open_time"):
        if candidate in df.columns:
            time_col = candidate
            break
    if time_col is None:
        raise ValueError(f"Futures candlestick response missing time/open_time. Keys={list(df.columns)}")

    time_values = pd.to_numeric(df[time_col], errors="coerce")
    time_unit = "ms" if float(time_values.dropna().median()) > 1e11 else "s"
    df[time_col] = pd.to_datetime(time_values, unit=time_unit, utc=True).dt.tz_convert(IST)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=[time_col, "open", "high", "low", "close", "volume"])
    df = df.sort_values(time_col).drop_duplicates(subset=[time_col], keep="last")
    df = df.rename(
        columns={
            time_col: "time",
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        }
    ).set_index("time")
    df = df[df.index <= latest_closed_open]
    return df[["Open", "High", "Low", "Close", "Volume"]], latest_closed_open


def _to_ts_from_trade_series(ts_values: pd.Series) -> pd.Series:
    sample = pd.to_numeric(ts_values, errors="coerce").dropna()
    if sample.empty:
        return pd.to_datetime(sample, unit="ms", utc=True)
    med = float(sample.median())
    unit = "ms" if med > 1e11 else "s"
    return pd.to_datetime(pd.to_numeric(ts_values, errors="coerce"), unit=unit, utc=True)


def _fetch_pair_trades(pair: str, limit: int = 500) -> pd.DataFrame:
    url = f"{PUBLIC_BASE}/market_data/trade_history"
    res = requests.get(url, params={"pair": pair, "limit": min(limit, 500)}, timeout=20)
    res.raise_for_status()
    trades = res.json()
    if not trades:
        return pd.DataFrame(columns=["price", "qty"])

    df = pd.DataFrame(trades)
    expected = {"p", "q", "T"}
    missing = expected - set(df.columns)
    if missing:
        raise ValueError(f"Trade response missing keys: {sorted(missing)}")

    df["time"] = _to_ts_from_trade_series(df["T"]).dt.tz_convert(IST)
    df["price"] = pd.to_numeric(df["p"], errors="coerce")
    df["qty"] = pd.to_numeric(df["q"], errors="coerce")
    df = df.dropna(subset=["time", "price", "qty"]).sort_values("time")
    return df[["time", "price", "qty"]]


def _fetch_futures_trades(pair: str, limit: int = 500) -> pd.DataFrame:
    url = f"{PRIVATE_BASE}/exchange/v1/derivatives/futures/data/trades"
    res = requests.get(url, params={"pair": pair}, timeout=20)
    res.raise_for_status()
    trades = res.json()
    if not trades:
        return pd.DataFrame(columns=["time", "price", "qty"])

    df = pd.DataFrame(trades)
    if "timestamp" not in df.columns or "price" not in df.columns or "quantity" not in df.columns:
        raise ValueError("Futures trades response missing required keys")
    df["time"] = pd.to_datetime(pd.to_numeric(df["timestamp"], errors="coerce"), unit="ms", utc=True).dt.tz_convert(IST)
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["qty"] = pd.to_numeric(df["quantity"], errors="coerce")
    df = df.dropna(subset=["time", "price", "qty"]).sort_values("time")
    if limit and len(df) > limit:
        df = df.iloc[-limit:]
    return df[["time", "price", "qty"]]


def _build_candles_from_trades(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    candles = (
        trades.set_index("time")
        .resample(BAR_FREQ, label="left", closed="left")
        .agg(
            Open=("price", "first"),
            High=("price", "max"),
            Low=("price", "min"),
            Close=("price", "last"),
            Volume=("qty", "sum"),
        )
        .dropna(subset=["Open", "High", "Low", "Close"])
    )
    return candles


def _freshness_minutes(ts: pd.Timestamp | None) -> float:
    if ts is None or pd.isna(ts):
        return float("inf")
    now_ist = pd.Timestamp.now(tz=IST)
    return abs((now_ist - ts).total_seconds()) / 60.0


def _fetch_futures_orderbook(pair: str) -> dict | None:
    url = f"{PUBLIC_BASE}/market_data/v3/orderbook/{pair}-futures/50"
    res = requests.get(url, timeout=20)
    res.raise_for_status()
    data = res.json()
    return data if isinstance(data, dict) else None


def fetch_market_ticker(market: str) -> dict | None:
    url = f"{PRIVATE_BASE}/exchange/ticker"
    res = requests.get(url, timeout=20)
    res.raise_for_status()
    data = res.json()
    if not isinstance(data, list):
        return None
    for row in data:
        if row.get("market") == market:
            return row
    return None


def fetch_futures_ticker(pair: str) -> dict | None:
    trades = _fetch_futures_trades(pair, limit=20)
    if trades.empty:
        return None
    last_price = float(trades.iloc[-1]["price"])
    out = {"last_price": last_price}
    try:
        book = _fetch_futures_orderbook(pair)
        if book:
            bids = book.get("bids") or {}
            asks = book.get("asks") or {}
            if bids:
                out["bid"] = float(max(bids.keys(), key=lambda x: float(x)))
            if asks:
                out["ask"] = float(min(asks.keys(), key=lambda x: float(x)))
    except Exception:
        pass
    return out


def cache_path_for_pair(pair: str, execution_mode: str) -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    cache_dir = os.path.join(here, ".cache")
    os.makedirs(cache_dir, exist_ok=True)
    safe_pair = pair.replace("-", "_")
    safe_mode = execution_mode.replace("-", "_")
    return os.path.join(cache_dir, f"live_confluence_{safe_mode}_{safe_pair}.csv")


def load_cached_bars(pair: str, execution_mode: str) -> pd.DataFrame:
    path = cache_path_for_pair(pair, execution_mode)
    if not os.path.exists(path):
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    try:
        df = pd.read_csv(path)
        if df.empty:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_convert(IST)
        df = df.set_index("time").sort_index()
        return df[["Open", "High", "Low", "Close", "Volume"]]
    except Exception:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])


def save_cached_bars(pair: str, execution_mode: str, df: pd.DataFrame) -> None:
    if df.empty:
        return
    path = cache_path_for_pair(pair, execution_mode)
    out = df.reset_index().rename(columns={"index": "time"})
    out["time"] = out["time"].dt.tz_convert("UTC")
    out.to_csv(path, index=False)


def merge_bars(old_df: pd.DataFrame, new_df: pd.DataFrame, keep: int = 2000) -> pd.DataFrame:
    frames = []
    if old_df is not None and not old_df.empty:
        frames.append(old_df)
    if new_df is not None and not new_df.empty:
        frames.append(new_df)
    if not frames:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    merged = pd.concat(frames).sort_index()
    merged = merged[~merged.index.duplicated(keep="last")]
    if len(merged) > keep:
        merged = merged.iloc[-keep:]
    return merged


def fetch_closed_bars(
    pair: str,
    market: str,
    lookback_days: int,
    limit: int = 1000,
    execution_mode: str = "spot",
) -> Tuple[pd.DataFrame, pd.Timestamp, str | None, str | None]:
    latest_closed_open = pd.Timestamp.now(tz=IST).floor(BAR_FREQ) - pd.Timedelta(minutes=BAR_MINUTES)
    best_pair = None
    best_df = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    best_source = None
    best_last_ts = None

    if execution_mode == "futures":
        df, latest_closed_open = _fetch_futures_candles(pair, lookback_days=lookback_days)
        if not df.empty:
            best_df = df
            best_pair = pair
            best_source = "futures_candles"
            best_last_ts = df.index.max()
        trades = _fetch_futures_trades(pair, limit=500)
        trade_candles = _build_candles_from_trades(trades)
        trade_candles = trade_candles[trade_candles.index <= latest_closed_open]
        if not trade_candles.empty:
            last_ts = trade_candles.index.max()
            if best_last_ts is None or last_ts > best_last_ts:
                best_df = trade_candles
                best_pair = pair
                best_source = "futures_trades"
                best_last_ts = last_ts
        return best_df, latest_closed_open, best_pair, best_source

    for candidate in candidate_candle_pairs(pair, market):
        # First try native candles.
        df, latest_closed_open = _fetch_pair_candles(candidate, lookback_days=lookback_days, limit=limit)
        if not df.empty:
            last_ts = df.index.max()
            if best_last_ts is None or last_ts > best_last_ts:
                best_df = df
                best_pair = candidate
                best_source = "candles"
                best_last_ts = last_ts

        # Then try building candles from trades, which are fresher for some markets.
        trades = _fetch_pair_trades(candidate, limit=500)
        trade_candles = _build_candles_from_trades(trades)
        trade_candles = trade_candles[trade_candles.index <= latest_closed_open]
        if not trade_candles.empty:
            last_ts = trade_candles.index.max()
            if best_last_ts is None or last_ts > best_last_ts:
                best_df = trade_candles
                best_pair = candidate
                best_source = "trades"
                best_last_ts = last_ts

    return best_df, latest_closed_open, best_pair, best_source


def _fmt_ts(ts: pd.Timestamp) -> str:
    return ts.strftime("%Y-%m-%d %H:%M")


def _fmt_side(side: int) -> str:
    if side == 1:
        return "LONG"
    if side == -1:
        return "SHORT"
    return "FLAT"


def _hmac_signature(payload_json: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload_json.encode("utf-8"), hashlib.sha256).hexdigest()


def _log_private_request(method: str, path: str, body: dict, params: dict | None = None) -> None:
    parts = [f"[API {method}] {path}", f"payload={json.dumps(body, separators=(',', ':'))}"]
    if params:
        parts.append(f"params={json.dumps(params, separators=(',', ':'))}")
    print(" | ".join(parts))


def _private_post(path: str, body: dict, cfg: RuntimeConfig) -> dict:
    if not cfg.api_key or not cfg.api_secret:
        raise ValueError("Missing API key/secret")

    payload = json.dumps(body, separators=(",", ":"))
    sig = _hmac_signature(payload, cfg.api_secret)
    headers = {
        "Content-Type": "application/json",
        "X-AUTH-APIKEY": cfg.api_key,
        "X-AUTH-SIGNATURE": sig,
    }
    url = f"{PRIVATE_BASE}{path}"
    _log_private_request("POST", path, body)
    resp = requests.post(url, data=payload, headers=headers, timeout=20)
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}
    print(f"[API POST] {path} | status={resp.status_code} | response={data}")

    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status_code} {data}")
    return data


def _private_get(path: str, body: dict, cfg: RuntimeConfig, params: dict | None = None) -> dict:
    if not cfg.api_key or not cfg.api_secret:
        raise ValueError("Missing API key/secret")

    payload = json.dumps(body, separators=(",", ":"))
    sig = _hmac_signature(payload, cfg.api_secret)
    headers = {
        "Content-Type": "application/json",
        "X-AUTH-APIKEY": cfg.api_key,
        "X-AUTH-SIGNATURE": sig,
    }
    url = f"{PRIVATE_BASE}{path}"
    _log_private_request("GET", path, body, params=params)
    resp = requests.get(url, params=params, data=payload, headers=headers, timeout=20)
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}
    print(f"[API GET] {path} | status={resp.status_code} | response={data}")

    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status_code} {data}")
    return data


def _round_qty(qty: float, precision: int) -> float:
    return float(np.round(qty, precision))


def _round_down_to_increment(value: float, increment: float) -> float:
    if increment <= 0:
        return value
    return float(np.floor(value / increment) * increment)


def _round_up_to_increment(value: float, increment: float) -> float:
    if increment <= 0:
        return value
    return float(np.ceil(value / increment) * increment)


def _increment_decimals(increment: float) -> int:
    if increment <= 0:
        return 8
    text = f"{increment:.12f}".rstrip("0").rstrip(".")
    if "." not in text:
        return 0
    return len(text.split(".", 1)[1])


def _now_ms() -> int:
    return int(round(time.time() * 1000))


def _quote_currency_from_market(market: str) -> str | None:
    for suffix in ("USDT", "USDC", "INR", "BTC", "ETH", "USD"):
        if market.endswith(suffix):
            return suffix
    return None


def _risk_per_unit(entry_px: float, stop_px: float) -> float:
    risk_per_unit = abs(entry_px - stop_px)
    if risk_per_unit < 1e-9:
        risk_per_unit = max(abs(entry_px) * 0.001, 1e-6)
    return risk_per_unit


def _desired_qty(entry_px: float, stop_px: float, risk_dollars: float) -> float:
    return risk_dollars / _risk_per_unit(entry_px, stop_px)


def _derive_brackets_with_meta(
    side: int, entry_px: float, stop_anchor: float
) -> tuple[float, float, float, str] | None:
    if not np.isfinite(entry_px) or not np.isfinite(stop_anchor):
        return None

    min_gap = max(abs(entry_px) * 0.001, 1e-6)

    if side == 1:
        if stop_anchor < entry_px - 1e-9:
            stop_px = stop_anchor
            risk_per_unit = entry_px - stop_px
            source = "supertrend"
        else:
            risk_per_unit = max(stop_anchor - entry_px, min_gap)
            stop_px = entry_px - risk_per_unit
            source = "mirrored"
        target_px = entry_px + 2 * risk_per_unit
        return stop_px, target_px, risk_per_unit, source

    if side == -1:
        if stop_anchor > entry_px + 1e-9:
            stop_px = stop_anchor
            risk_per_unit = stop_px - entry_px
            source = "supertrend"
        else:
            risk_per_unit = max(entry_px - stop_anchor, min_gap)
            stop_px = entry_px + risk_per_unit
            source = "mirrored"
        target_px = entry_px - 2 * risk_per_unit
        return stop_px, target_px, risk_per_unit, source

    return None


def _derive_brackets(side: int, entry_px: float, stop_anchor: float) -> tuple[float, float, float] | None:
    brackets = _derive_brackets_with_meta(side, entry_px, stop_anchor)
    if brackets is None:
        return None
    stop_px, target_px, risk_per_unit, _ = brackets
    return stop_px, target_px, risk_per_unit


def _normalize_futures_brackets(side: int, stop_px: float, target_px: float, cfg: RuntimeConfig) -> tuple[float, float, int] | None:
    instrument = fetch_futures_instrument_details(cfg.pair, cfg.futures_margin_currency)
    price_increment = float(
        instrument.get("price_increment")
        or instrument.get("tick_size")
        or instrument.get("min_price_increment")
        or 0.01
    )
    price_dp = _increment_decimals(price_increment)

    if side == 1:
        stop_px = _round_down_to_increment(stop_px, price_increment)
        target_px = _round_down_to_increment(target_px, price_increment)
    elif side == -1:
        stop_px = _round_up_to_increment(stop_px, price_increment)
        target_px = _round_up_to_increment(target_px, price_increment)
    else:
        return None

    return stop_px, target_px, price_dp


def _reset_open_trade_fields(state: TradeState) -> None:
    state.side = 0
    state.entry_ts = None
    state.entry_px = np.nan
    state.stop_px = np.nan
    state.target_px = np.nan
    state.qty = np.nan
    state.init_risk_per_unit = np.nan
    state.broker_order_id = None
    state.broker_order_status = None
    state.exit_pending = False
    state.position_id = None


def fetch_balances(cfg: RuntimeConfig) -> List[dict]:
    return _private_post("/exchange/v1/users/balances", {"timestamp": _now_ms()}, cfg)


def fetch_futures_wallets(cfg: RuntimeConfig) -> List[dict]:
    data = _private_get("/exchange/v1/derivatives/futures/wallets", {"timestamp": _now_ms()}, cfg)
    if not isinstance(data, list):
        raise ValueError("Futures wallets response is not a list")
    return data


def fetch_futures_instrument_details(pair: str, margin_currency: str) -> dict:
    url = f"{PRIVATE_BASE}/exchange/v1/derivatives/futures/data/instrument"
    res = requests.get(
        url,
        params={"pair": pair, "margin_currency_short_name": margin_currency},
        timeout=20,
    )
    res.raise_for_status()
    data = res.json()
    instrument = data.get("instrument") if isinstance(data, dict) else None
    if not isinstance(instrument, dict):
        raise ValueError("Futures instrument response missing instrument data")
    return instrument


def fetch_futures_position(pair: str, cfg: RuntimeConfig) -> dict | None:
    body = {
        "timestamp": _now_ms(),
        "page": "1",
        "size": "10",
        "pairs": pair,
        "margin_currency_short_name": [cfg.futures_margin_currency],
    }
    data = _private_post("/exchange/v1/derivatives/futures/positions", body, cfg)
    if not isinstance(data, list) or not data:
        return None
    for row in data:
        if str(row.get("pair")) == pair:
            return row
    return None


def wait_for_active_futures_position(
    pair: str,
    cfg: RuntimeConfig,
    retries: int = 8,
    sleep_seconds: float = 0.5,
) -> dict | None:
    last_position = None
    for _ in range(max(retries, 1)):
        position = fetch_futures_position(pair, cfg)
        if position:
            last_position = position
            active_pos = float(position.get("active_pos") or 0.0)
            if abs(active_pos) > 1e-12:
                return position
        time.sleep(max(sleep_seconds, 0.0))
    return last_position


def fetch_available_quote_balance(cfg: RuntimeConfig) -> Tuple[float | None, str | None]:
    if cfg.execution_mode == "futures":
        wallets = fetch_futures_wallets(cfg)
        for row in wallets:
            if str(row.get("currency_short_name", "")).upper() != cfg.futures_margin_currency:
                continue
            available = float(row.get("balance") or 0.0)
            return max(available, 0.0), cfg.futures_margin_currency
        return 0.0, cfg.futures_margin_currency

    quote = _quote_currency_from_market(cfg.market)
    if not quote:
        return None, None
    balances = fetch_balances(cfg)
    if not isinstance(balances, list):
        return None, quote
    for row in balances:
        if str(row.get("currency", "")).upper() != quote:
            continue
        balance = float(row.get("balance") or 0.0)
        locked = float(row.get("locked_balance") or 0.0)
        return max(balance - locked, 0.0), quote
    return 0.0, quote


def _cap_entry_qty(side: int, desired_qty: float, price: float, cfg: RuntimeConfig) -> Tuple[float, str | None]:
    if not cfg.place_orders:
        return desired_qty, None

    try:
        available_quote, quote = fetch_available_quote_balance(cfg)
    except Exception as exc:
        return 0.0, f"Could not fetch balances for live sizing: {exc}"
    if available_quote is None or quote is None:
        return 0.0, "Could not determine available quote balance for live sizing."

    if cfg.execution_mode == "margin":
        max_notional = available_quote * max(cfg.leverage, 1.0)
    elif cfg.execution_mode == "futures":
        instrument = fetch_futures_instrument_details(cfg.pair, cfg.futures_margin_currency)
        max_notional = available_quote * max(cfg.leverage, 1.0)
        max_qty = max_notional / price if price > 0 else 0.0
        max_qty = min(max_qty, float(instrument.get("max_market_order_quantity") or max_qty))
        qty_increment = float(instrument.get("quantity_increment") or 0.0)
        min_qty = max(
            float(instrument.get("min_quantity") or 0.0),
            float(instrument.get("min_trade_size") or 0.0),
        )
        capped_qty = _round_down_to_increment(min(desired_qty, max_qty), qty_increment)
        if capped_qty <= 0:
            return 0.0, f"Available {quote} balance is too low for a futures order."
        if capped_qty < min_qty:
            return 0.0, (
                f"Capped futures quantity {capped_qty:,.8f} is below minimum quantity {min_qty:,.8f} "
                f"for {cfg.pair}."
            )
        min_notional = float(instrument.get("min_notional") or 0.0)
        if capped_qty * price < min_notional:
            return 0.0, (
                f"Futures order value ${capped_qty * price:,.4f} is below min notional ${min_notional:,.4f} "
                f"for {cfg.pair}."
            )
        if capped_qty + 1e-12 < desired_qty:
            return (
                capped_qty,
                f"Qty capped by available {quote} futures wallet balance: desired={desired_qty:,.8f}, "
                f"capped={capped_qty:,.8f}, available={available_quote:,.4f} {quote}, leverage={cfg.leverage:.2f}x.",
            )
        return capped_qty, None
    else:
        if side == -1:
            return 0.0, "Live short execution requires margin mode."
        max_notional = available_quote

    max_qty = max_notional / price if price > 0 else 0.0
    capped_qty = min(desired_qty, max_qty)
    if capped_qty <= 0:
        return 0.0, f"Available {quote} balance is too low for a live order."
    if capped_qty + 1e-12 < desired_qty:
        return (
            capped_qty,
            f"Qty capped by available {quote} balance: desired={desired_qty:,.8f}, capped={capped_qty:,.8f}, "
            f"available={available_quote:,.4f} {quote}.",
        )
    return capped_qty, None


def _build_market_order(side: str, market: str, qty: float) -> dict:
    return {
        "side": side,
        "order_type": "market_order",
        "market": market,
        "total_quantity": qty,
        "timestamp": _now_ms(),
        "client_order_id": f"lc-{_now_ms()}",
    }


def _place_market_order(side: str, qty: float, cfg: RuntimeConfig) -> Tuple[bool, str]:
    if qty <= 0:
        return False, "invalid qty <= 0"
    qty = _round_qty(qty, cfg.qty_precision)
    if qty <= 0:
        return False, "rounded qty <= 0"

    if not cfg.place_orders:
        return True, f"PAPER order side={side} qty={qty}"

    try:
        body = _build_market_order(side=side, market=cfg.market, qty=qty)
        data = _private_post("/exchange/v1/orders/create", body, cfg)
        return True, f"LIVE order side={side} qty={qty} response={data}"
    except Exception as exc:
        return False, f"order failed: {exc}"


def _build_margin_order(
    side: str,
    market: str,
    qty: float,
    stop_px: float,
    target_px: float,
    cfg: RuntimeConfig,
) -> dict:
    return {
        "side": side,
        "order_type": "market_order",
        "market": market,
        "quantity": qty,
        "leverage": cfg.leverage,
        "target_price": target_px,
        "sl_price": stop_px,
        "ecode": cfg.margin_ecode,
        "timestamp": _now_ms(),
    }


def _extract_margin_order(data: object) -> dict | None:
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            return first
    if isinstance(data, dict):
        return data
    return None


def _place_margin_order(
    side: str,
    qty: float,
    stop_px: float,
    target_px: float,
    cfg: RuntimeConfig,
) -> Tuple[bool, str, str | None]:
    if qty <= 0:
        return False, "invalid qty <= 0", None
    qty = _round_qty(qty, cfg.qty_precision)
    if qty <= 0:
        return False, "rounded qty <= 0", None
    if not cfg.place_orders:
        return True, f"PAPER margin order side={side} qty={qty}", None

    try:
        body = _build_margin_order(side=side, market=cfg.market, qty=qty, stop_px=stop_px, target_px=target_px, cfg=cfg)
        data = _private_post("/exchange/v1/margin/create", body, cfg)
        order = _extract_margin_order(data)
        order_id = str(order.get("id")) if order and order.get("id") else None
        return True, f"LIVE margin order side={side} qty={qty} response={data}", order_id
    except Exception as exc:
        return False, f"margin order failed: {exc}", None


def _margin_edit_sl(order_id: str, stop_px: float, cfg: RuntimeConfig) -> Tuple[bool, str]:
    if not cfg.place_orders:
        return True, f"PAPER margin edit_sl id={order_id} sl={stop_px:,.4f}"
    try:
        data = _private_post(
            "/exchange/v1/margin/edit_sl",
            {"id": order_id, "sl_price": stop_px, "timestamp": _now_ms()},
            cfg,
        )
        return True, f"LIVE margin edit_sl id={order_id} sl={stop_px:,.4f} response={data}"
    except Exception as exc:
        return False, f"margin edit_sl failed: {exc}"


def _margin_exit(order_id: str, cfg: RuntimeConfig) -> Tuple[bool, str]:
    if not cfg.place_orders:
        return True, f"PAPER margin exit id={order_id}"
    try:
        data = _private_post("/exchange/v1/margin/exit", {"id": order_id, "timestamp": _now_ms()}, cfg)
        return True, f"LIVE margin exit id={order_id} response={data}"
    except Exception as exc:
        return False, f"margin exit failed: {exc}"


def _fetch_margin_order(order_id: str, cfg: RuntimeConfig) -> dict | None:
    data = _private_post("/exchange/v1/margin/order", {"id": order_id, "details": True, "timestamp": _now_ms()}, cfg)
    return _extract_margin_order(data)


def _extract_futures_order(data: object) -> dict | None:
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            return first
    if isinstance(data, dict) and "id" in data:
        return data
    return None


def _build_futures_order(
    side: str,
    qty: float,
    cfg: RuntimeConfig,
) -> dict:
    order = {
        "side": side,
        "pair": cfg.pair,
        "order_type": "market_order",
        "price": None,
        "stop_price": None,
        "total_quantity": qty,
        "leverage": cfg.leverage,
        "notification": "no_notification",
        "hidden": False,
        "post_only": False,
        "margin_currency_short_name": cfg.futures_margin_currency,
        "position_margin_type": cfg.position_margin_type,
    }
    return {"timestamp": _now_ms(), "order": order}


def _place_futures_order(
    side: str,
    qty: float,
    cfg: RuntimeConfig,
) -> Tuple[bool, str, str | None]:
    if qty <= 0:
        return False, "invalid qty <= 0", None
    qty = _round_qty(qty, cfg.qty_precision)
    if qty <= 0:
        return False, "rounded qty <= 0", None
    if not cfg.place_orders:
        return True, f"PAPER futures order side={side} qty={qty}", None
    try:
        body = _build_futures_order(side=side, qty=qty, cfg=cfg)
        data = _private_post("/exchange/v1/derivatives/futures/orders/create", body, cfg)
        order = _extract_futures_order(data)
        order_id = str(order.get("id")) if order and order.get("id") else None
        return True, f"LIVE futures order side={side} qty={qty} response={data}", order_id
    except Exception as exc:
        return False, f"futures order failed: {exc}", None


def _futures_exit_position(position_id: str, cfg: RuntimeConfig) -> Tuple[bool, str]:
    if not cfg.place_orders:
        return True, f"PAPER futures exit position_id={position_id}"
    try:
        data = _private_post(
            "/exchange/v1/derivatives/futures/positions/exit",
            {"timestamp": _now_ms(), "id": position_id},
            cfg,
        )
        return True, f"LIVE futures exit position_id={position_id} response={data}"
    except Exception as exc:
        return False, f"futures exit failed: {exc}"


def _futures_create_tpsl(position_id: str, stop_px: float, target_px: float, cfg: RuntimeConfig) -> Tuple[bool, str]:
    if not cfg.place_orders:
        return True, (
            f"PAPER futures create_tpsl position_id={position_id} stop={stop_px:,.4f} target={target_px:,.4f}"
        )
    side = 1 if target_px >= stop_px else -1
    normalized = _normalize_futures_brackets(side, stop_px, target_px, cfg)
    if normalized is None:
        return False, "futures create_tpsl failed: invalid normalized brackets"
    stop_px, target_px, price_dp = normalized
    body = {
        "timestamp": _now_ms(),
        "id": position_id,
        "take_profit": {
            "stop_price": f"{target_px:.{price_dp}f}",
            "order_type": "take_profit_market",
        },
        "stop_loss": {
            "stop_price": f"{stop_px:.{price_dp}f}",
            "order_type": "stop_market",
        },
    }
    try:
        data = _private_post("/exchange/v1/derivatives/futures/positions/create_tpsl", body, cfg)
        return True, f"LIVE futures create_tpsl position_id={position_id} response={data}"
    except Exception as exc:
        return False, f"futures create_tpsl failed: {exc}"


def _set_resume_tpsl_from_history(
    ts: pd.Timestamp,
    frame: pd.DataFrame,
    state: TradeState,
    cfg: RuntimeConfig,
    lookback_bars: int = 12,
) -> List[str]:
    if cfg.execution_mode != "futures" or state.side == 0 or not state.position_id:
        return []

    hist = frame.loc[frame.index <= ts].tail(max(lookback_bars, 1))
    if hist.empty:
        return [f"[{_fmt_ts(ts)}] RESUME TP/SL SKIPPED | no historical bars available."]

    if state.side == 1:
        stop_anchor = float(hist["Low"].min())
    else:
        stop_anchor = float(hist["High"].max())

    brackets_meta = _derive_brackets_with_meta(state.side, state.entry_px, stop_anchor)
    if brackets_meta is None:
        return [
            f"[{_fmt_ts(ts)}] RESUME TP/SL SKIPPED | invalid 1h brackets "
            f"Entry={state.entry_px:,.4f} StopAnchor={stop_anchor:,.4f}"
        ]

    stop_px, target_px, _resume_risk, source = brackets_meta
    normalized = _normalize_futures_brackets(state.side, stop_px, target_px, cfg)
    if normalized is None:
        return [f"[{_fmt_ts(ts)}] RESUME TP/SL SKIPPED | could not normalize 1h brackets."]

    stop_px, target_px, _ = normalized
    state.stop_px = stop_px
    state.target_px = target_px
    state.init_risk_per_unit = _risk_per_unit(state.entry_px, state.stop_px)

    ok, msg = _futures_create_tpsl(state.position_id, state.stop_px, state.target_px, cfg)
    events = [
        f"[{_fmt_ts(ts)}] RESUME {_fmt_side(state.side)} 1H TP/SL | "
        f"Bars={len(hist)} Source={source} Entry={state.entry_px:,.4f} "
        f"SL={state.stop_px:,.4f} Target={state.target_px:,.4f}"
    ]
    if ok:
        events.append(f"[{_fmt_ts(ts)}] {msg}")
    else:
        events.append(f"[{_fmt_ts(ts)}] RESUME TP/SL FAILED | {msg}")
    return events


def _enter_trade(
    state: TradeState,
    side: int,
    ts: pd.Timestamp,
    px: float,
    st_line: float,
    risk_dollars: float,
    qty_override: float | None = None,
) -> str:
    risk_per_unit = _risk_per_unit(px, st_line)
    qty = qty_override if qty_override is not None else risk_dollars / risk_per_unit
    target = px + 2 * risk_per_unit if side == 1 else px - 2 * risk_per_unit
    notional = qty * px

    state.side = side
    state.trade_id += 1
    state.entry_ts = ts
    state.entry_px = px
    state.stop_px = st_line
    state.target_px = target
    state.qty = qty
    state.init_risk_per_unit = risk_per_unit

    return (
        f"[{_fmt_ts(ts)}] ENTRY {_fmt_side(side)} | Trade #{state.trade_id} | "
        f"Entry={px:,.4f} SL={state.stop_px:,.4f} Target={state.target_px:,.4f} | "
        f"Qty={state.qty:,.8f} (Risk ${risk_dollars:.2f}, Notional ${notional:,.2f})"
    )


def _exit_trade(state: TradeState, ts: pd.Timestamp, exit_px: float, reason: str, risk_dollars: float) -> str:
    if state.side == 1:
        pnl = (exit_px - state.entry_px) * state.qty
    else:
        pnl = (state.entry_px - exit_px) * state.qty
    r_mult = pnl / risk_dollars if risk_dollars > 0 else np.nan
    state.realized_pnl += pnl

    msg = (
        f"[{_fmt_ts(ts)}] EXIT {_fmt_side(state.side)} | Trade #{state.trade_id} | "
        f"Exit={exit_px:,.4f} Reason={reason} | PnL=${pnl:,.2f} ({r_mult:+.2f}R) | "
        f"Cumulative=${state.realized_pnl:,.2f}"
    )

    _reset_open_trade_fields(state)
    return msg


def sync_margin_order_state(ts: pd.Timestamp, state: TradeState, cfg: RuntimeConfig) -> List[str]:
    if (
        state.side == 0
        or not cfg.place_orders
        or cfg.execution_mode != "margin"
        or not state.broker_order_id
    ):
        return []

    events: List[str] = []
    try:
        order = _fetch_margin_order(state.broker_order_id, cfg)
    except Exception as exc:
        return [f"[{_fmt_ts(ts)}] BROKER SYNC FAILED | {exc}"]

    if not order:
        return [f"[{_fmt_ts(ts)}] BROKER SYNC FAILED | Empty response for margin order {state.broker_order_id}"]

    status = str(order.get("status", "")).lower()
    state.broker_order_status = status

    avg_entry = float(order.get("avg_entry") or 0.0)
    if avg_entry > 0:
        state.entry_px = avg_entry

    if status in {"close", "cancelled", "rejected"}:
        avg_exit = float(order.get("avg_exit") or 0.0)
        broker_pnl = float(order.get("pnl") or 0.0)
        state.realized_pnl += broker_pnl
        exit_text = f"{avg_exit:,.4f}" if avg_exit > 0 else "n/a"
        events.append(
            f"[{_fmt_ts(ts)}] BROKER {_fmt_side(state.side)} CLOSED | "
            f"Status={status.upper()} OrderId={state.broker_order_id} Exit={exit_text} "
            f"BrokerPnL=${broker_pnl:,.2f} Cumulative=${state.realized_pnl:,.2f}"
        )
        _reset_open_trade_fields(state)
    return events


def sync_futures_position_state(ts: pd.Timestamp, state: TradeState, cfg: RuntimeConfig) -> List[str]:
    if cfg.execution_mode != "futures":
        return []

    try:
        position = fetch_futures_position(cfg.pair, cfg)
    except Exception as exc:
        if cfg.place_orders:
            return [f"[{_fmt_ts(ts)}] FUTURES POSITION SYNC FAILED | {exc}"]
        return []

    if not position:
        return []

    events: List[str] = []
    position_id = str(position.get("id")) if position.get("id") else None
    active_pos = float(position.get("active_pos") or 0.0)
    avg_price = float(position.get("avg_price") or 0.0)
    stop_trigger = float(position.get("stop_loss_trigger") or 0.0)
    take_trigger = float(position.get("take_profit_trigger") or 0.0)

    prev_side = state.side

    if abs(active_pos) < 1e-12:
        if state.side != 0:
            events.append(
                f"[{_fmt_ts(ts)}] BROKER {_fmt_side(state.side)} CLOSED | "
                f"PositionId={state.position_id or 'n/a'} Pair={cfg.pair}"
            )
            _reset_open_trade_fields(state)
            state.position_id = position_id
        elif state.position_id and state.position_id == position_id:
            state.position_id = None
        return events

    if position_id:
        state.position_id = position_id
    state.side = 1 if active_pos > 0 else -1
    state.qty = abs(active_pos)
    if avg_price > 0:
        state.entry_px = avg_price
    if stop_trigger > 0:
        state.stop_px = stop_trigger
    if take_trigger > 0:
        state.target_px = take_trigger
    state.broker_order_status = "open"
    if prev_side == 0:
        events.append(
            f"[{_fmt_ts(ts)}] BROKER {_fmt_side(state.side)} DETECTED | "
            f"PositionId={state.position_id or 'n/a'} Entry={state.entry_px:,.4f} "
            f"SL={state.stop_px:,.4f} Target={state.target_px:,.4f} Qty={state.qty:,.8f}"
        )
    return events


def process_closed_bar_futures(ts: pd.Timestamp, row: pd.Series, state: TradeState, cfg: RuntimeConfig) -> List[str]:
    score = int(row["score"])
    close = float(row["Close"])
    high = float(row["High"])
    low = float(row["Low"])
    st_line = float(row["st_line"])
    events: List[str] = []

    if not cfg.place_orders:
        return process_closed_bar(ts, row, state, RuntimeConfig(**{**cfg.__dict__, "execution_mode": "spot"}))

    if state.side == 0:
        signal_side = 0
        if score >= LONG_ENTRY_SCORE:
            signal_side = 1
        elif score <= SHORT_ENTRY_SCORE:
            if not cfg.allow_shorts:
                events.append(f"[{_fmt_ts(ts)}] SHORT SIGNAL score={score:+d} ignored (futures shorts disabled).")
                return events
            signal_side = -1
        if signal_side == 0:
            return events

        preview_brackets_meta = _derive_brackets_with_meta(signal_side, close, st_line)
        preview_brackets = (
            preview_brackets_meta[:3] if preview_brackets_meta is not None else None
        )
        if preview_brackets is None:
            events.append(
                f"[{_fmt_ts(ts)}] {_fmt_side(signal_side)} ENTRY BLOCKED | "
                f"invalid strategy brackets Entry={close:,.4f} StopAnchor={st_line:,.4f}"
            )
            return events

        desired_qty = _desired_qty(close, st_line, cfg.risk_dollars)
        entry_qty, cap_msg = _cap_entry_qty(signal_side, desired_qty, close, cfg)
        if entry_qty <= 0:
            events.append(
                f"[{_fmt_ts(ts)}] {_fmt_side(signal_side)} ENTRY BLOCKED | {cap_msg or 'qty <= 0'}"
            )
            return events

        preview = TradeState(
            side=state.side,
            trade_id=state.trade_id,
            entry_ts=state.entry_ts,
            entry_px=state.entry_px,
            stop_px=state.stop_px,
            target_px=state.target_px,
            qty=state.qty,
            init_risk_per_unit=state.init_risk_per_unit,
            realized_pnl=state.realized_pnl,
            position_id=state.position_id,
        )
        enter_msg = _enter_trade(preview, signal_side, ts, close, st_line, cfg.risk_dollars, qty_override=entry_qty)
        order_side = "buy" if signal_side == 1 else "sell"
        ok, broker_msg, broker_order_id = _place_futures_order(order_side, preview.qty, cfg)
        if not ok:
            events.append(f"[{_fmt_ts(ts)}] {_fmt_side(signal_side)} ENTRY BLOCKED | {broker_msg}")
            return events
        if preview_brackets_meta and preview_brackets_meta[3] == "mirrored":
            events.append(
                f"[{_fmt_ts(ts)}] {_fmt_side(signal_side)} BRACKET FALLBACK | "
                f"Stop anchor {st_line:,.4f} was on the wrong side of entry {close:,.4f}; "
                "using mirrored protective stop."
            )
        state.__dict__.update(preview.__dict__)
        state.broker_order_id = broker_order_id
        state.broker_order_status = "initial" if broker_order_id else None
        state.exit_pending = False
        position = wait_for_active_futures_position(cfg.pair, cfg)
        if position:
            state.position_id = str(position.get("id")) if position.get("id") else state.position_id
            active_pos = float(position.get("active_pos") or 0.0)
            if abs(active_pos) > 1e-12:
                state.qty = abs(active_pos)
                state.side = 1 if active_pos > 0 else -1
                avg_price = float(position.get("avg_price") or 0.0)
                if avg_price > 0:
                    state.entry_px = avg_price
        live_brackets = _derive_brackets(state.side, state.entry_px, st_line)
        events.append(enter_msg)
        if cap_msg:
            events.append(f"[{_fmt_ts(ts)}] {cap_msg}")
        events.append(f"[{_fmt_ts(ts)}] {broker_msg}")
        if not state.position_id or not position or abs(float(position.get("active_pos") or 0.0)) <= 1e-12:
            events.append(
                f"[{_fmt_ts(ts)}] {_fmt_side(state.side)} TP/SL PENDING | position not active yet, waiting for futures position sync."
            )
            return events
        if live_brackets is None:
            ok_exit, exit_msg = _futures_exit_position(state.position_id, cfg)
            if ok_exit:
                state.exit_pending = True
                state.broker_order_status = "exit_pending"
                events.append(
                    f"[{_fmt_ts(ts)}] {_fmt_side(state.side)} TP/SL FAILED | "
                    f"invalid live brackets Entry={state.entry_px:,.4f} StopAnchor={st_line:,.4f}. Exit sent."
                )
                events.append(f"[{_fmt_ts(ts)}] {exit_msg}")
            else:
                events.append(
                    f"[{_fmt_ts(ts)}] {_fmt_side(state.side)} TP/SL FAILED | "
                    f"invalid live brackets Entry={state.entry_px:,.4f} StopAnchor={st_line:,.4f}. "
                    f"Manual check needed: {exit_msg}"
                )
            return events
        normalized_live_brackets = _normalize_futures_brackets(state.side, live_brackets[0], live_brackets[1], cfg)
        if normalized_live_brackets is None:
            events.append(f"[{_fmt_ts(ts)}] {_fmt_side(state.side)} TP/SL FAILED | invalid normalized brackets.")
            return events
        state.stop_px, state.target_px, _ = normalized_live_brackets
        state.init_risk_per_unit = _risk_per_unit(state.entry_px, state.stop_px)
        ok_tpsl, tpsl_msg = _futures_create_tpsl(state.position_id, state.stop_px, state.target_px, cfg)
        if not ok_tpsl:
            ok_exit, exit_msg = _futures_exit_position(state.position_id, cfg)
            if ok_exit:
                state.exit_pending = True
                state.broker_order_status = "exit_pending"
                events.append(f"[{_fmt_ts(ts)}] {_fmt_side(state.side)} TP/SL ATTACH FAILED | {tpsl_msg}")
                events.append(f"[{_fmt_ts(ts)}] UNPROTECTED POSITION EXIT SENT | {exit_msg}")
            else:
                events.append(f"[{_fmt_ts(ts)}] {_fmt_side(state.side)} TP/SL ATTACH FAILED | {tpsl_msg}")
                events.append(f"[{_fmt_ts(ts)}] UNPROTECTED POSITION | Manual check needed: {exit_msg}")
            return events
        events.append(
            f"[{_fmt_ts(ts)}] {_fmt_side(state.side)} SL/TARGET ARMED | "
            f"PositionId={state.position_id} Entry={state.entry_px:,.4f} "
            f"SL={state.stop_px:,.4f} Target={state.target_px:,.4f}"
        )
        events.append(f"[{_fmt_ts(ts)}] {tpsl_msg}")
        return events

    if state.position_id and state.target_px > 0:
        next_stop = max(state.stop_px, st_line) if state.side == 1 else min(state.stop_px, st_line)
        normalized_next = _normalize_futures_brackets(state.side, next_stop, state.target_px, cfg)
        if normalized_next is not None:
            next_stop_norm, next_target_norm, _ = normalized_next
        else:
            next_stop_norm, next_target_norm = next_stop, state.target_px
        moved = (
            next_stop_norm > state.stop_px + 1e-9 if state.side == 1 else next_stop_norm < state.stop_px - 1e-9
        )
        if moved:
            if _derive_brackets(state.side, state.entry_px, next_stop_norm) is None:
                events.append(
                    f"[{_fmt_ts(ts)}] TRAIL {_fmt_side(state.side)} SKIPPED | "
                    f"candidate stop {next_stop_norm:,.4f} crosses entry {state.entry_px:,.4f}"
                )
                return events
            ok, broker_msg = _futures_create_tpsl(state.position_id, next_stop_norm, next_target_norm, cfg)
            if ok:
                state.stop_px = next_stop_norm
                state.target_px = next_target_norm
                if "remains same as existing" in broker_msg.lower():
                    events.append(f"[{_fmt_ts(ts)}] TRAIL {_fmt_side(state.side)} UNCHANGED | SL={state.stop_px:,.4f}")
                else:
                    events.append(f"[{_fmt_ts(ts)}] TRAIL {_fmt_side(state.side)} SL -> {state.stop_px:,.4f}")
                events.append(f"[{_fmt_ts(ts)}] {broker_msg}")
            else:
                events.append(f"[{_fmt_ts(ts)}] TRAIL {_fmt_side(state.side)} FAILED | {broker_msg}")

    should_exit = None
    if state.side == 1 and score < LONG_EXIT_SCORE:
        should_exit = "SIGNAL"
    elif state.side == -1 and score > SHORT_EXIT_SCORE:
        should_exit = "SIGNAL"

    if should_exit and state.position_id:
        if state.exit_pending:
            events.append(f"[{_fmt_ts(ts)}] EXIT {_fmt_side(state.side)} PENDING ({should_exit}) | waiting for broker close.")
            return events
        ok, broker_msg = _futures_exit_position(state.position_id, cfg)
        if not ok:
            events.append(f"[{_fmt_ts(ts)}] EXIT {_fmt_side(state.side)} FAILED ({should_exit}) | {broker_msg}")
            return events
        state.exit_pending = True
        state.broker_order_status = "exit_pending"
        events.append(f"[{_fmt_ts(ts)}] EXIT {_fmt_side(state.side)} SENT ({should_exit}) | PositionId={state.position_id}")
        events.append(f"[{_fmt_ts(ts)}] {broker_msg}")
    elif state.position_id:
        if state.side == 1 and low <= state.stop_px:
            events.append(f"[{_fmt_ts(ts)}] LONG SL/TARGET managed on exchange | waiting for futures position sync.")
        elif state.side == -1 and high >= state.stop_px:
            events.append(f"[{_fmt_ts(ts)}] SHORT SL/TARGET managed on exchange | waiting for futures position sync.")

    return events


def process_closed_bar(ts: pd.Timestamp, row: pd.Series, state: TradeState, cfg: RuntimeConfig) -> List[str]:
    if cfg.execution_mode == "futures":
        return process_closed_bar_futures(ts, row, state, cfg)

    score = int(row["score"])
    close = float(row["Close"])
    high = float(row["High"])
    low = float(row["Low"])
    st_line = float(row["st_line"])
    events: List[str] = []

    if state.side == 0:
        if score >= LONG_ENTRY_SCORE:
            preview = TradeState(
                side=state.side,
                trade_id=state.trade_id,
                entry_ts=state.entry_ts,
                entry_px=state.entry_px,
                stop_px=state.stop_px,
                target_px=state.target_px,
                qty=state.qty,
                init_risk_per_unit=state.init_risk_per_unit,
                realized_pnl=state.realized_pnl,
            )
            desired_qty = _desired_qty(close, st_line, cfg.risk_dollars)
            entry_qty, cap_msg = _cap_entry_qty(1, desired_qty, close, cfg)
            if entry_qty <= 0:
                events.append(f"[{_fmt_ts(ts)}] LONG ENTRY BLOCKED | {cap_msg or 'qty <= 0'}")
                return events
            enter_msg = _enter_trade(preview, 1, ts, close, st_line, cfg.risk_dollars, qty_override=entry_qty)
            if cfg.execution_mode == "margin":
                ok, broker_msg, broker_order_id = _place_margin_order(
                    "buy", preview.qty, preview.stop_px, preview.target_px, cfg
                )
            else:
                ok, broker_msg = _place_market_order("buy", preview.qty, cfg)
                broker_order_id = None
            if not ok:
                events.append(f"[{_fmt_ts(ts)}] LONG ENTRY BLOCKED | {broker_msg}")
                return events
            state.__dict__.update(preview.__dict__)
            state.broker_order_id = broker_order_id
            state.broker_order_status = "init" if broker_order_id else None
            state.exit_pending = False
            events.append(enter_msg)
            if cap_msg:
                events.append(f"[{_fmt_ts(ts)}] {cap_msg}")
            events.append(f"[{_fmt_ts(ts)}] {broker_msg}")
        elif score <= SHORT_ENTRY_SCORE:
            if not cfg.allow_shorts:
                events.append(f"[{_fmt_ts(ts)}] SHORT SIGNAL score={score:+d} ignored (spot-only mode).")
                return events
            preview = TradeState(
                side=state.side,
                trade_id=state.trade_id,
                entry_ts=state.entry_ts,
                entry_px=state.entry_px,
                stop_px=state.stop_px,
                target_px=state.target_px,
                qty=state.qty,
                init_risk_per_unit=state.init_risk_per_unit,
                realized_pnl=state.realized_pnl,
            )
            desired_qty = _desired_qty(close, st_line, cfg.risk_dollars)
            entry_qty, cap_msg = _cap_entry_qty(-1, desired_qty, close, cfg)
            if entry_qty <= 0:
                events.append(f"[{_fmt_ts(ts)}] SHORT ENTRY BLOCKED | {cap_msg or 'qty <= 0'}")
                return events
            enter_msg = _enter_trade(preview, -1, ts, close, st_line, cfg.risk_dollars, qty_override=entry_qty)
            if cfg.execution_mode == "margin":
                ok, broker_msg, broker_order_id = _place_margin_order(
                    "sell", preview.qty, preview.stop_px, preview.target_px, cfg
                )
            else:
                ok, broker_msg = _place_market_order("sell", preview.qty, cfg)
                broker_order_id = None
            if not ok:
                events.append(f"[{_fmt_ts(ts)}] SHORT ENTRY BLOCKED | {broker_msg}")
                return events
            state.__dict__.update(preview.__dict__)
            state.broker_order_id = broker_order_id
            state.broker_order_status = "init" if broker_order_id else None
            state.exit_pending = False
            events.append(enter_msg)
            if cap_msg:
                events.append(f"[{_fmt_ts(ts)}] {cap_msg}")
            events.append(f"[{_fmt_ts(ts)}] {broker_msg}")
        return events

    if state.side == 1:
        prev_stop = state.stop_px
        next_stop = max(state.stop_px, st_line)
        if next_stop > prev_stop + 1e-9:
            if cfg.execution_mode == "margin" and cfg.place_orders and state.broker_order_id:
                ok, broker_msg = _margin_edit_sl(state.broker_order_id, next_stop, cfg)
                if ok:
                    state.stop_px = next_stop
                    events.append(f"[{_fmt_ts(ts)}] TRAIL LONG SL -> {state.stop_px:,.4f}")
                    events.append(f"[{_fmt_ts(ts)}] {broker_msg}")
                else:
                    events.append(f"[{_fmt_ts(ts)}] TRAIL LONG FAILED | {broker_msg}")
            else:
                state.stop_px = next_stop
                events.append(f"[{_fmt_ts(ts)}] TRAIL LONG SL -> {state.stop_px:,.4f}")

        should_exit = None
        exit_px = close
        if low <= state.stop_px:
            should_exit = "STOP"
            exit_px = state.stop_px
        elif high >= state.target_px:
            should_exit = "TARGET"
            exit_px = state.target_px
        elif score < LONG_EXIT_SCORE:
            should_exit = "SIGNAL"
            exit_px = close

        if should_exit:
            if cfg.execution_mode == "margin" and cfg.place_orders and state.broker_order_id:
                events.extend(sync_margin_order_state(ts, state, cfg))
                if state.side == 0:
                    return events
                if state.exit_pending:
                    events.append(f"[{_fmt_ts(ts)}] EXIT LONG PENDING ({should_exit}) | waiting for broker close.")
                    return events
                ok, broker_msg = _margin_exit(state.broker_order_id, cfg)
                if not ok:
                    events.append(f"[{_fmt_ts(ts)}] EXIT LONG FAILED ({should_exit}) | {broker_msg}")
                    return events
                state.exit_pending = True
                state.broker_order_status = "exit_pending"
                events.append(f"[{_fmt_ts(ts)}] EXIT LONG SENT ({should_exit}) | BrokerOrder={state.broker_order_id}")
                events.append(f"[{_fmt_ts(ts)}] {broker_msg}")
                return events
            ok, broker_msg = _place_market_order("sell", state.qty, cfg)
            if not ok:
                events.append(f"[{_fmt_ts(ts)}] EXIT LONG FAILED ({should_exit}) | {broker_msg}")
                return events
            events.append(_exit_trade(state, ts, exit_px, should_exit, cfg.risk_dollars))
            events.append(f"[{_fmt_ts(ts)}] {broker_msg}")
            return events

    if state.side == -1:
        prev_stop = state.stop_px
        next_stop = min(state.stop_px, st_line)
        if next_stop < prev_stop - 1e-9:
            if cfg.execution_mode == "margin" and cfg.place_orders and state.broker_order_id:
                ok, broker_msg = _margin_edit_sl(state.broker_order_id, next_stop, cfg)
                if ok:
                    state.stop_px = next_stop
                    events.append(f"[{_fmt_ts(ts)}] TRAIL SHORT SL -> {state.stop_px:,.4f}")
                    events.append(f"[{_fmt_ts(ts)}] {broker_msg}")
                else:
                    events.append(f"[{_fmt_ts(ts)}] TRAIL SHORT FAILED | {broker_msg}")
            else:
                state.stop_px = next_stop
                events.append(f"[{_fmt_ts(ts)}] TRAIL SHORT SL -> {state.stop_px:,.4f}")

        should_exit = None
        exit_px = close
        if high >= state.stop_px:
            should_exit = "STOP"
            exit_px = state.stop_px
        elif low <= state.target_px:
            should_exit = "TARGET"
            exit_px = state.target_px
        elif score > SHORT_EXIT_SCORE:
            should_exit = "SIGNAL"
            exit_px = close

        if should_exit:
            if cfg.execution_mode == "margin" and cfg.place_orders and state.broker_order_id:
                events.extend(sync_margin_order_state(ts, state, cfg))
                if state.side == 0:
                    return events
                if state.exit_pending:
                    events.append(f"[{_fmt_ts(ts)}] EXIT SHORT PENDING ({should_exit}) | waiting for broker close.")
                    return events
                ok, broker_msg = _margin_exit(state.broker_order_id, cfg)
                if not ok:
                    events.append(f"[{_fmt_ts(ts)}] EXIT SHORT FAILED ({should_exit}) | {broker_msg}")
                    return events
                state.exit_pending = True
                state.broker_order_status = "exit_pending"
                events.append(f"[{_fmt_ts(ts)}] EXIT SHORT SENT ({should_exit}) | BrokerOrder={state.broker_order_id}")
                events.append(f"[{_fmt_ts(ts)}] {broker_msg}")
                return events
            ok, broker_msg = _place_market_order("buy", state.qty, cfg)
            if not ok:
                events.append(f"[{_fmt_ts(ts)}] EXIT SHORT FAILED ({should_exit}) | {broker_msg}")
                return events
            events.append(_exit_trade(state, ts, exit_px, should_exit, cfg.risk_dollars))
            events.append(f"[{_fmt_ts(ts)}] {broker_msg}")
            return events

    return events


def print_bar_heartbeat(ts: pd.Timestamp, row: pd.Series, state: TradeState) -> None:
    side = _fmt_side(state.side)
    rsi = float(row["rsi"])
    score = int(row["score"])
    close = float(row["Close"])
    if state.side == 0:
        print(f"[{_fmt_ts(ts)}] BAR Close={close:,.4f} Score={score:+d} RSI={rsi:5.1f} Position={side}")
    else:
        if state.side == 1:
            pnl_pct = (close / state.entry_px - 1) * 100
        else:
            pnl_pct = (state.entry_px / close - 1) * 100
        print(
            f"[{_fmt_ts(ts)}] BAR Close={close:,.4f} Score={score:+d} RSI={rsi:5.1f} "
            f"Position={side} Entry={state.entry_px:,.4f} SL={state.stop_px:,.4f} "
            f"Target={state.target_px:,.4f} LivePnL={pnl_pct:+.2f}%"
        )


def print_price_status(
    ts: pd.Timestamp,
    bars: pd.DataFrame,
    used_pair: str | None,
    used_source: str | None,
    ticker: dict | None,
) -> None:
    if bars.empty:
        return
    last = bars.iloc[-1]
    closed_close = float(last["Close"])
    high = float(last["High"])
    low = float(last["Low"])
    vol = float(last["Volume"])
    parts = [
        f"[{_fmt_ts(ts)}] PRICE",
        f"Current={closed_close:,.4f}",
        f"ClosedBar={closed_close:,.4f}",
        f"High={high:,.4f}",
        f"Low={low:,.4f}",
        f"Volume={vol:,.6f}",
        f"Feed={used_pair}/{used_source}",
    ]
    if ticker:
        try:
            parts[1] = f"Current={float(ticker.get('last_price')):,.4f}"
            parts.append(f"Bid={float(ticker.get('bid')):,.4f}")
            parts.append(f"Ask={float(ticker.get('ask')):,.4f}")
        except Exception:
            pass
    print(" ".join(parts))


def run_monitor(cfg: RuntimeConfig) -> None:
    requested_pair = cfg.pair
    if cfg.execution_mode == "futures":
        cfg.pair = requested_pair
        cfg.market = derive_market_from_pair(cfg.pair)
    else:
        _, resolved_market = resolve_pair(cfg.pair, cfg.market)
        if resolved_market != cfg.market:
            print(f"Market auto-resolved: {cfg.market} -> {resolved_market}")
        cfg.pair = requested_pair
        cfg.market = resolved_market

    if cfg.place_orders:
        if cfg.execution_mode != "futures":
            pair_market_suffix = _pair_suffix(cfg.pair)
            desired_market_suffix = derive_suffix_from_market(cfg.market)
            if pair_market_suffix != desired_market_suffix:
                raise ValueError(
                    f"Execution safety check failed: pair {cfg.pair} does not match market {cfg.market}. "
                    f"Use paper mode or align data/execution market first."
                )
        if cfg.allow_shorts and cfg.execution_mode not in {"margin", "futures"}:
            raise ValueError(
                "Live short execution requires futures or margin mode. Re-run with --execution-mode futures "
                "or remove --allow-shorts for spot execution."
            )

    state = TradeState()
    last_processed_ts: pd.Timestamp | None = None

    print("=" * 96)
    print(
        f"Live Confluence Monitor | Pair={cfg.pair} | Market={cfg.market} | TF={TIMEFRAME} | "
        f"Mode={cfg.execution_mode.upper()} | "
        f"Risk=${cfg.risk_dollars:.2f} | PlaceOrders={cfg.place_orders}"
    )
    print("=" * 96)
    print("Rules: long entry score>=+3, long exit score<+2, short entry score<=-3, short exit score>-2")
    if cfg.place_orders:
        print("LIVE ORDER MODE ENABLED.")
    else:
        print("Paper mode enabled. No exchange orders will be placed.")
    if not cfg.allow_shorts and cfg.execution_mode == "spot":
        print("Spot-only mode: short entries are ignored.")
    if cfg.execution_mode == "margin":
        print(f"Margin execution enabled. Leverage={cfg.leverage:.2f} Ecode={cfg.margin_ecode}")
    if cfg.execution_mode == "futures":
        print(
            f"Futures execution enabled. MarginCurrency={cfg.futures_margin_currency} "
            f"MarginType={cfg.position_margin_type} Leverage={cfg.leverage:.2f}"
        )

    while True:
        try:
            bars, latest_closed, used_pair, used_source = fetch_closed_bars(
                cfg.pair,
                cfg.market,
                lookback_days=cfg.lookback_days,
                execution_mode=cfg.execution_mode,
            )
            if used_pair and (used_pair != cfg.candle_pair or used_source != cfg.data_source):
                print(f"Data feed selected: pair={used_pair} source={used_source}")
                cfg.candle_pair = used_pair
                cfg.data_source = used_source
            if used_pair:
                cached_bars = load_cached_bars(used_pair, cfg.execution_mode)
                bars = merge_bars(cached_bars, bars)
                bars = bars[bars.index <= latest_closed]
                save_cached_bars(used_pair, cfg.execution_mode, bars)
            if cfg.place_orders and cfg.execution_mode != "futures" and used_pair and _pair_suffix(used_pair) != derive_suffix_from_market(cfg.market):
                raise ValueError(
                    f"Execution safety check failed: freshest data pair {used_pair} does not match live market {cfg.market}. "
                    f"Disable --place-orders or choose a matching market."
                )
            if bars.empty:
                tried = ", ".join(candidate_candle_pairs(cfg.pair, cfg.market))
                print(f"No closed bars fetched. Tried candle pairs: {tried}. Retrying...")
                time.sleep(cfg.poll_seconds)
                continue

            ticker = None
            try:
                ticker = fetch_futures_ticker(cfg.pair) if cfg.execution_mode == "futures" else fetch_market_ticker(cfg.market)
            except Exception:
                ticker = None
            print_price_status(bars.index.max(), bars, used_pair, used_source, ticker)

            last_bar_ts = bars.index.max()
            if latest_closed - last_bar_ts > pd.Timedelta(minutes=BAR_MINUTES * 6):
                print(
                    f"Stale data guard: latest bar={_fmt_ts(last_bar_ts)} IST, "
                    f"expected around {_fmt_ts(latest_closed)} IST. "
                    f"Feed pair={used_pair} source={used_source}. Skipping trading cycle."
                )
                time.sleep(cfg.poll_seconds)
                continue

            frame = build_confluence_frame(bars)
            if frame.empty:
                bar_count = len(bars)
                first_ts = _fmt_ts(bars.index.min()) if not bars.empty else "n/a"
                last_ts = _fmt_ts(bars.index.max()) if not bars.empty else "n/a"
                print(
                    f"Warmup in progress: {bar_count}/{MIN_WARMUP_BARS}+ bars collected "
                    f"(first={first_ts}, last={last_ts}, feed={used_pair}/{used_source}). Retrying..."
                )
                time.sleep(cfg.poll_seconds)
                continue

            if last_processed_ts is None:
                # Bootstrap from history in paper mode only (never submit historical live orders).
                boot_cfg = RuntimeConfig(**{**cfg.__dict__, "place_orders": False})
                for ts, row in frame.iterrows():
                    process_closed_bar(ts, row, state, boot_cfg)
                    last_processed_ts = ts
                last_row = frame.iloc[-1]
                print(
                    f"Bootstrapped to {_fmt_ts(last_processed_ts)} IST "
                    f"(latest closed={_fmt_ts(latest_closed)} IST)."
                )
                if cfg.place_orders:
                    state = TradeState(realized_pnl=state.realized_pnl)
                    print("Live mode starts flat after bootstrap; historical bars are used for indicator warmup only.")
                    for ev in sync_futures_position_state(last_processed_ts, state, cfg):
                        print(ev)
                    for ev in sync_margin_order_state(last_processed_ts, state, cfg):
                        print(ev)
                    if state.side != 0 and state.entry_ts is None:
                        state.entry_ts = last_processed_ts
                        print(
                            f"[{_fmt_ts(last_processed_ts)}] Resuming {_fmt_side(state.side)} management "
                            "from existing broker position."
                        )
                        for ev in _set_resume_tpsl_from_history(last_processed_ts, frame, state, cfg):
                            print(ev)
                print_bar_heartbeat(last_processed_ts, last_row, state)
                if cfg.place_orders:
                    startup_events = process_closed_bar(last_processed_ts, last_row, state, cfg)
                    for ev in startup_events:
                        print(ev)
            else:
                new_rows = frame[frame.index > last_processed_ts]
                for ts, row in new_rows.iterrows():
                    for ev in sync_futures_position_state(ts, state, cfg):
                        print(ev)
                    for ev in sync_margin_order_state(ts, state, cfg):
                        print(ev)
                    print_bar_heartbeat(ts, row, state)
                    events = process_closed_bar(ts, row, state, cfg)
                    for ev in events:
                        print(ev)
                    last_processed_ts = ts

            time.sleep(cfg.poll_seconds)
        except KeyboardInterrupt:
            print("\nStopped by user.")
            break
        except Exception as exc:
            print(f"Error: {exc}. Retrying in {cfg.poll_seconds}s...")
            time.sleep(cfg.poll_seconds)


def parse_args() -> RuntimeConfig:
    parser = argparse.ArgumentParser(
        description="CoinDCX live 5m confluence monitor with fixed-dollar risk sizing"
    )
    parser.add_argument("--pair", type=str, default="B-ETH_USDT", help="CoinDCX pair for candles, e.g. B-ETH_USDT")
    parser.add_argument(
        "--market",
        type=str,
        default=None,
        help="CoinDCX market for order API, e.g. ETHUSDT (default derives from pair)",
    )
    parser.add_argument("--risk", type=float, default=100.0, help="Dollar risk per trade (default: 100)")
    parser.add_argument("--poll", type=int, default=15, help="Polling interval seconds (default: 15)")
    parser.add_argument("--place-orders", action="store_true", help="Enable live order placement on CoinDCX")
    parser.add_argument("--allow-shorts", action="store_true", help="Allow short entries (disabled by default)")
    parser.add_argument(
        "--execution-mode",
        choices=["auto", "spot", "margin", "futures"],
        default="auto",
        help="Execution mode for live orders: auto, spot, margin, or futures (default: auto)",
    )
    parser.add_argument("--leverage", type=float, default=1.0, help="Margin leverage for margin mode (default: 1)")
    parser.add_argument("--margin-ecode", type=str, default="B", help="CoinDCX margin ecode (default: B)")
    parser.add_argument(
        "--futures-margin-currency",
        type=str,
        default="USDT",
        help="CoinDCX futures wallet currency: USDT or INR (default: USDT)",
    )
    parser.add_argument(
        "--position-margin-type",
        choices=["crossed", "isolated"],
        default="crossed",
        help="Futures position margin type (default: crossed)",
    )
    parser.add_argument("--qty-precision", type=int, default=6, help="Decimal precision for order quantity")
    parser.add_argument("--lookback-days", type=int, default=7, help="Candle lookback window (days)")
    parser.add_argument("--api-key", type=str, default=None, help="API key (or use COINDCX_API_KEY env var)")
    parser.add_argument("--api-secret", type=str, default=None, help="API secret (or use COINDCX_API_SECRET env var)")
    args = parser.parse_args()

    market = args.market or derive_market_from_pair(args.pair)
    api_key = args.api_key or os.getenv("COINDCX_API_KEY")
    api_secret = args.api_secret or os.getenv("COINDCX_API_SECRET")
    execution_mode = args.execution_mode
    if execution_mode == "auto":
        execution_mode = "futures" if args.place_orders else "spot"

    cfg = RuntimeConfig(
        pair=args.pair,
        market=market,
        risk_dollars=float(args.risk),
        poll_seconds=int(args.poll),
        place_orders=bool(args.place_orders),
        allow_shorts=bool(args.allow_shorts),
        execution_mode=str(execution_mode),
        leverage=float(args.leverage),
        margin_ecode=str(args.margin_ecode).upper(),
        futures_margin_currency=str(args.futures_margin_currency).upper(),
        position_margin_type=str(args.position_margin_type).lower(),
        qty_precision=int(args.qty_precision),
        lookback_days=int(args.lookback_days),
        api_key=api_key,
        api_secret=api_secret,
    )

    if cfg.place_orders and (not cfg.api_key or not cfg.api_secret):
        raise ValueError(
            "Live order mode needs API credentials. Set --api-key/--api-secret "
            "or COINDCX_API_KEY/COINDCX_API_SECRET."
        )
    if cfg.execution_mode == "futures" and cfg.position_margin_type == "crossed" and cfg.futures_margin_currency != "USDT":
        raise ValueError("CoinDCX futures crossed margin is supported only for USDT-margined futures.")

    return cfg


def main() -> None:
    cfg = parse_args()
    run_monitor(cfg)


if __name__ == "__main__":
    main()
