#!/usr/bin/env python3
"""
Live Confluence Monitor (CoinDCX, IST, 15m)
==========================================
Continuously pulls CoinDCX 15m candles, computes confluence score, and emits
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
from pathlib import Path
from typing import List, Tuple

import numpy as np
import pandas as pd
import requests
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")

_pending_db_writes: list[dict] = []


def drain_completed_trades() -> list[dict]:
    global _pending_db_writes
    out, _pending_db_writes = _pending_db_writes, []
    return out

PUBLIC_BASE = "https://public.coindcx.com"
PRIVATE_BASE = "https://api.coindcx.com"

TIMEFRAME = "15m"
BAR_FREQ = "15min"
BAR_MINUTES = 15

# (minutes, group, max_lookback_days, recommended_lookback_days)
SUPPORTED_TIMEFRAMES: dict[str, tuple[int, str, int, int]] = {
    "1m":  (1,    "minutes", 3,   1),
    "3m":  (3,    "minutes", 7,   3),
    "5m":  (5,    "minutes", 14,  7),
    "15m": (15,   "minutes", 30,  14),
    "30m": (30,   "minutes", 60,  30),
    "1h":  (60,   "hours",   120, 60),
    "2h":  (120,  "hours",   180, 90),
    "4h":  (240,  "hours",   365, 180),
    "6h":  (360,  "hours",   365, 180),
    "12h": (720,  "hours",   365, 180),
    "1d":  (1440, "daily",   365, 365),
}

_TF_LABELS: dict[str, str] = {
    "1m": "1 Min", "3m": "3 Min", "5m": "5 Min",
    "15m": "15 Min", "30m": "30 Min",
    "1h": "1 Hour", "2h": "2 Hours", "4h": "4 Hours",
    "6h": "6 Hours", "12h": "12 Hours",
    "1d": "1 Day",
}


def list_timeframes() -> list[dict]:
    out: list[dict] = []
    for tf, (minutes, group, max_lb, rec_lb) in SUPPORTED_TIMEFRAMES.items():
        out.append({
            "id": tf,
            "label": _TF_LABELS.get(tf, tf),
            "minutes": minutes,
            "group": group,
            "max_lookback_days": max_lb,
            "recommended_lookback_days": rec_lb,
        })
    return out
MIN_WARMUP_BARS = 30

LONG_ENTRY_SCORE = 3
LONG_EXIT_SCORE = 2
SHORT_ENTRY_SCORE = -3
SHORT_EXIT_SCORE = -2


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file(Path(__file__).resolve().parents[1] / ".env")


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
    timeframe: str = TIMEFRAME
    candle_pair: str | None = None
    data_source: str | None = None
    api_key: str | None = None
    api_secret: str | None = None
    strategy: str | None = None


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


@dataclass
class TradePlan:
    side: int
    entry_px: float
    stop_px: float
    target_px: float
    risk_per_unit: float
    risk_amount: float
    qty: float
    notional: float
    leverage: float
    margin_required: float
    leverage_note: str | None
    support: float | None
    resistance: float | None
    rr: float
    structure_rr: float | None
    stop_source: str
    blocked_reason: str | None = None


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
    out["atr"] = atr_s(df)
    out["score"] = v_st + v_er + v_mc + v_bb + v_rs
    out["rsi"] = rsi
    out = out.dropna(subset=["st_line", "score", "rsi", "atr"])
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


def _normalize_timeframe(tf: str | None) -> tuple[str, str, int]:
    value = str(tf or TIMEFRAME).strip().lower()
    if value not in SUPPORTED_TIMEFRAMES:
        value = TIMEFRAME
    minutes = SUPPORTED_TIMEFRAMES[value][0]
    if minutes < 60:
        freq = f"{minutes}min"
    elif minutes < 1440:
        freq = f"{minutes // 60}h"
    else:
        freq = "D"
    return value, freq, minutes


def _fetch_pair_candles(pair: str, lookback_days: int, limit: int = 1000, timeframe: str = TIMEFRAME) -> Tuple[pd.DataFrame, pd.Timestamp]:
    url = f"{PUBLIC_BASE}/market_data/candles"
    now_utc = pd.Timestamp.now(tz="UTC")
    end_ms = int(now_utc.timestamp() * 1000)
    start_ms = int((now_utc - pd.Timedelta(days=max(1, lookback_days))).timestamp() * 1000)
    tf, bar_freq, bar_minutes = _normalize_timeframe(timeframe)
    params = {
        "pair": pair,
        "interval": tf,
        "limit": min(limit, 1000),
        "startTime": start_ms,
        "endTime": end_ms,
    }
    res = requests.get(url, params=params, timeout=20)
    res.raise_for_status()
    candles = res.json()

    now_ist = pd.Timestamp.now(tz=IST)
    latest_closed_open = now_ist.floor(bar_freq) - pd.Timedelta(minutes=bar_minutes)

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


def _fetch_futures_candles(pair: str, lookback_days: int, timeframe: str = TIMEFRAME) -> Tuple[pd.DataFrame, pd.Timestamp]:
    url = f"{PUBLIC_BASE}/market_data/candlesticks"
    now_utc = pd.Timestamp.now(tz="UTC")
    now_sec = int(now_utc.timestamp())
    start_sec = int((now_utc - pd.Timedelta(days=max(1, lookback_days))).timestamp())
    tf, bar_freq, bar_minutes = _normalize_timeframe(timeframe)
    params = {
        "pair": pair,
        "from": start_sec,
        "to": now_sec,
        "resolution": _futures_resolution(tf),
        "pcode": "f",
    }
    res = requests.get(url, params=params, timeout=20)
    res.raise_for_status()
    raw = res.json()

    now_ist = pd.Timestamp.now(tz=IST)
    latest_closed_open = now_ist.floor(bar_freq) - pd.Timedelta(minutes=bar_minutes)
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


def _build_candles_from_trades(trades: pd.DataFrame, bar_freq: str = BAR_FREQ) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    candles = (
        trades.set_index("time")
        .resample(bar_freq, label="left", closed="left")
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


def cache_path_for_pair(pair: str, execution_mode: str, timeframe: str | None = None) -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    cache_dir = os.path.join(here, ".cache")
    os.makedirs(cache_dir, exist_ok=True)
    safe_pair = pair.replace("-", "_")
    safe_mode = execution_mode.replace("-", "_")
    tf, _, _ = _normalize_timeframe(timeframe)
    safe_tf = tf.replace("-", "_")
    return os.path.join(cache_dir, f"live_confluence_{safe_mode}_{safe_pair}_{safe_tf}.csv")


def load_cached_bars(pair: str, execution_mode: str, timeframe: str | None = None) -> pd.DataFrame:
    path = cache_path_for_pair(pair, execution_mode, timeframe=timeframe)
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


def save_cached_bars(pair: str, execution_mode: str, df: pd.DataFrame, timeframe: str | None = None) -> None:
    if df.empty:
        return
    path = cache_path_for_pair(pair, execution_mode, timeframe=timeframe)
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
    timeframe: str | None = None,
) -> Tuple[pd.DataFrame, pd.Timestamp, str | None, str | None]:
    tf, bar_freq, bar_minutes = _normalize_timeframe(timeframe)
    latest_closed_open = pd.Timestamp.now(tz=IST).floor(bar_freq) - pd.Timedelta(minutes=bar_minutes)
    best_pair = None
    best_df = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    best_source = None
    best_last_ts = None

    if execution_mode == "futures":
        df, latest_closed_open = _fetch_futures_candles(pair, lookback_days=lookback_days, timeframe=tf)
        if not df.empty:
            best_df = df
            best_pair = pair
            best_source = "futures_candles"
            best_last_ts = df.index.max()
        trades = _fetch_futures_trades(pair, limit=500)
        trade_candles = _build_candles_from_trades(trades, bar_freq=bar_freq)
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
        df, latest_closed_open = _fetch_pair_candles(candidate, lookback_days=lookback_days, limit=limit, timeframe=tf)
        if not df.empty:
            last_ts = df.index.max()
            if best_last_ts is None or last_ts > best_last_ts:
                best_df = df
                best_pair = candidate
                best_source = "candles"
                best_last_ts = last_ts

        # Then try building candles from trades, which are fresher for some markets.
        trades = _fetch_pair_trades(candidate, limit=500)
        trade_candles = _build_candles_from_trades(trades, bar_freq=bar_freq)
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


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default


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


def _effective_leverage(cfg: RuntimeConfig) -> float:
    max_leverage = max(_env_float("MAX_LEVERAGE", cfg.leverage), 1.0)
    min_leverage = max(_env_float("MIN_LEVERAGE", 1.0), 1.0)
    return max(min(cfg.leverage, max_leverage), min_leverage)


def _leverage_limits(cfg: RuntimeConfig) -> tuple[float, float]:
    min_leverage = max(_env_float("MIN_LEVERAGE", 1.0), 1.0)
    max_leverage = max(_env_float("MAX_LEVERAGE", cfg.leverage), 1.0)
    return min_leverage, max_leverage


def _dynamic_leverage_for_plan(
    desired_qty: float,
    entry_px: float,
    cfg: RuntimeConfig,
    instrument: dict | None,
    available_quote: float | None,
    rr: float,
    structure_rr: float | None,
) -> tuple[float, str | None]:
    min_lev, max_lev = _leverage_limits(cfg)
    base_lev = _effective_leverage(cfg)
    max_from_cfg = max_lev
    if instrument:
        inst_max = max(
            float(instrument.get("max_leverage_long") or 0.0),
            float(instrument.get("max_leverage_short") or 0.0),
        )
        if inst_max > 0:
            max_from_cfg = min(max_from_cfg, inst_max)

    notional = max(desired_qty * entry_px, 0.0)
    note_parts: list[str] = []

    if structure_rr is not None and rr > 0:
        min_structure_rr = max(_env_float("MIN_STRUCTURE_RR", rr), 0.1)
        if structure_rr < min_structure_rr:
            rr_scale = max(structure_rr / min_structure_rr, 0.1)
            note_parts.append(f"rr_scale={rr_scale:.2f}")
            max_from_cfg = max(min_lev, max_from_cfg * rr_scale)

    if available_quote is None or available_quote <= 0:
        note_parts.append("balance unavailable")
        leverage = max(min(base_lev, max_from_cfg), min_lev)
        note = ", ".join(note_parts) if note_parts else None
        return leverage, note

    max_margin_pct = max(_env_float("MAX_MARGIN_PCT", 1.0), 0.05)
    max_margin_pct = min(max_margin_pct, 1.0)
    allowed_margin = max(available_quote * max_margin_pct, 1e-9)
    required = max(min_lev, notional / allowed_margin)
    note_parts.append(f"margin<= {max_margin_pct:.0%}")

    leverage = max(min(required, max_from_cfg), min_lev)
    note = ", ".join(note_parts) if note_parts else None
    return leverage, note


def _target_from_stop(side: int, entry_px: float, stop_px: float, rr: float) -> float:
    risk = _risk_per_unit(entry_px, stop_px)
    return entry_px + rr * risk if side == 1 else entry_px - rr * risk


def _finite_price(value: object) -> float | None:
    try:
        price = float(value)
    except Exception:
        return None
    return price if np.isfinite(price) and price > 0 else None


def _swing_prices(recent: pd.DataFrame, column: str, pivot_span: int, find_high: bool) -> list[tuple[float, int]]:
    values = [_finite_price(v) for v in recent[column].tolist()]
    swings: list[tuple[float, int]] = []
    for i in range(pivot_span, len(values) - pivot_span):
        price = values[i]
        if price is None:
            continue
        window = [v for v in values[i - pivot_span:i + pivot_span + 1] if v is not None]
        if len(window) < pivot_span + 1:
            continue
        edge_values = [v for j, v in enumerate(window) if j != pivot_span]
        if find_high and price >= max(window) and price > max(edge_values):
            swings.append((price, i))
        elif not find_high and price <= min(window) and price < min(edge_values):
            swings.append((price, i))
    return swings


def _cluster_structure_zones(
    recent: pd.DataFrame,
    swings: list[tuple[float, int]],
    touch_column: str,
    tolerance: float,
    min_touches: int,
) -> list[dict[str, float]]:
    if not swings:
        return []

    clusters: list[dict[str, float]] = []
    for price, idx in sorted(swings, key=lambda item: item[0]):
        for cluster in clusters:
            if abs(price - cluster["level"]) <= tolerance:
                cluster["weighted_sum"] += price
                cluster["count"] += 1
                cluster["latest_idx"] = max(cluster["latest_idx"], idx)
                cluster["level"] = cluster["weighted_sum"] / cluster["count"]
                break
        else:
            clusters.append({
                "level": price,
                "weighted_sum": price,
                "count": 1,
                "latest_idx": float(idx),
            })

    touch_values = [_finite_price(v) for v in recent[touch_column].tolist()]
    max_idx = max(len(touch_values) - 1, 1)
    zones: list[dict[str, float]] = []
    for cluster in clusters:
        level = cluster["level"]
        touches = sum(1 for value in touch_values if value is not None and abs(value - level) <= tolerance)
        if touches < min_touches:
            continue
        recency = cluster["latest_idx"] / max_idx
        zones.append({
            "level": level,
            "touches": float(touches),
            "score": float(touches) + recency,
        })
    return zones


def _nearest_support_resistance(
    history: pd.DataFrame | None,
    entry_px: float,
    lookback: int,
    atr: float | None = None,
) -> tuple[float | None, float | None]:
    if history is None or history.empty:
        return None, None
    recent = history.dropna(subset=["High", "Low"]).tail(max(lookback, 2))
    if recent.empty:
        return None, None

    pivot_span = max(_env_int("SUPPORT_RESISTANCE_PIVOT_SPAN", 2), 1)
    min_touches = max(_env_int("SUPPORT_RESISTANCE_MIN_TOUCHES", 2), 1)
    zone_pct = max(_env_float("SUPPORT_RESISTANCE_ZONE_PCT", 0.0015), 0.0001)
    zone_atr = max(_env_float("SUPPORT_RESISTANCE_ZONE_ATR", 0.50), 0.0)
    min_distance_pct = max(_env_float("SUPPORT_RESISTANCE_MIN_DISTANCE_PCT", 0.0005), 0.0)
    min_distance_atr = max(_env_float("SUPPORT_RESISTANCE_MIN_DISTANCE_ATR", 0.50), 0.0)

    atr_value = atr if atr is not None and np.isfinite(atr) and atr > 0 else 0.0
    tolerance = max(entry_px * zone_pct, atr_value * zone_atr, 1e-6)
    min_distance = max(entry_px * min_distance_pct, atr_value * min_distance_atr, tolerance * 0.5, 1e-6)

    low_swings = _swing_prices(recent, "Low", pivot_span, find_high=False)
    high_swings = _swing_prices(recent, "High", pivot_span, find_high=True)
    support_zones = _cluster_structure_zones(recent, low_swings, "Low", tolerance, min_touches)
    resistance_zones = _cluster_structure_zones(recent, high_swings, "High", tolerance, min_touches)

    support_candidates = [
        z for z in support_zones
        if z["level"] < entry_px - min_distance
    ]
    resistance_candidates = [
        z for z in resistance_zones
        if z["level"] > entry_px + min_distance
    ]

    support = max(support_candidates, key=lambda z: (z["level"], z["score"]))["level"] if support_candidates else None
    resistance = min(resistance_candidates, key=lambda z: (z["level"], -z["score"]))["level"] if resistance_candidates else None
    return support, resistance


def _resolve_risk_amount(cfg: RuntimeConfig) -> tuple[float, str | None]:
    pct = _env_float("RISK_PER_TRADE_PCT", 0.0)
    if pct <= 0 or not cfg.place_orders:
        return cfg.risk_dollars, None
    try:
        available, currency = fetch_available_quote_balance(cfg)
    except Exception as exc:
        return 0.0, f"Could not fetch wallet balance for percent risk sizing: {exc}"
    if available is None or available <= 0:
        return 0.0, "Wallet balance unavailable for percent risk sizing."
    risk_amount = available * (pct / 100.0)
    max_risk = _env_float("MAX_RISK_PER_TRADE", 0.0)
    if max_risk > 0:
        risk_amount = min(risk_amount, max_risk)
    return risk_amount, f"Risk={pct:.2f}% of available {currency or 'quote'} balance"


def _build_trade_plan(
    side: int,
    ts: pd.Timestamp,
    row: pd.Series,
    history: pd.DataFrame | None,
    cfg: RuntimeConfig,
) -> TradePlan:
    entry = float(row["Close"])
    st_line = float(row.get("st_line", np.nan))
    atr = float(row.get("atr", np.nan))
    if not np.isfinite(atr) or atr <= 0:
        atr = max(entry * 0.002, 1e-6)

    rr = max(_env_float("RISK_REWARD_RATIO", 2.0), 1.0)
    lookback = max(_env_int("SUPPORT_RESISTANCE_LOOKBACK", 72), 10)
    stop_buffer_atr = max(_env_float("STOP_BUFFER_ATR", 0.25), 0.0)
    min_stop_pct = max(_env_float("MIN_STOP_PCT", 0.001), 0.0001)
    min_gap = max(entry * min_stop_pct, atr * 0.10, 1e-6)

    support, resistance = _nearest_support_resistance(history, entry, lookback, atr)
    risk_amount, risk_msg = _resolve_risk_amount(cfg)
    if risk_amount <= 0:
        return TradePlan(side, entry, entry, entry, 0.0, risk_amount, 0.0, 0.0, _effective_leverage(cfg), 0.0,
                         None, support, resistance, rr, None, "none", risk_msg or "Risk amount is <= 0")

    stop_source = "mirrored"
    if side == 1:
        if support is not None:
            stop_px = support - atr * stop_buffer_atr
            stop_source = "support"
        elif np.isfinite(st_line) and st_line < entry:
            stop_px = st_line
            stop_source = "supertrend"
        else:
            stop_px = entry - min_gap
            stop_source = "minimum_gap"
        if stop_px >= entry:
            stop_px = entry - min_gap
            stop_source = "minimum_gap"
        risk_per_unit = _risk_per_unit(entry, stop_px)
        target_px = entry + rr * risk_per_unit
        structure_rr = ((resistance - entry) / risk_per_unit) if resistance is not None else None
    else:
        if resistance is not None:
            stop_px = resistance + atr * stop_buffer_atr
            stop_source = "resistance"
        elif np.isfinite(st_line) and st_line > entry:
            stop_px = st_line
            stop_source = "supertrend"
        else:
            stop_px = entry + min_gap
            stop_source = "minimum_gap"
        if stop_px <= entry:
            stop_px = entry + min_gap
            stop_source = "minimum_gap"
        risk_per_unit = _risk_per_unit(entry, stop_px)
        target_px = entry - rr * risk_per_unit
        structure_rr = ((entry - support) / risk_per_unit) if support is not None else None

    blocked = None
    min_structure_rr = max(_env_float("MIN_STRUCTURE_RR", rr), 0.1)
    if structure_rr is not None and structure_rr < min_structure_rr:
        blocked = (
            f"nearest {'resistance' if side == 1 else 'support'} offers only {structure_rr:.2f}R; "
            f"minimum required is {min_structure_rr:.2f}R"
        )

    qty = risk_amount / risk_per_unit if risk_per_unit > 0 else 0.0
    notional = qty * entry
    leverage = _effective_leverage(cfg)
    leverage_note = None
    if cfg.execution_mode in {"margin", "futures"}:
        instrument = None
        available_quote = None
        if cfg.execution_mode == "futures":
            try:
                instrument = fetch_futures_instrument_details(cfg.pair, cfg.futures_margin_currency)
            except Exception:
                instrument = None
        if cfg.place_orders:
            try:
                available_quote, _ = fetch_available_quote_balance(cfg)
            except Exception:
                available_quote = None
        leverage, leverage_note = _dynamic_leverage_for_plan(
            qty, entry, cfg, instrument, available_quote, rr, structure_rr
        )
    margin_required = notional / leverage if leverage > 0 else notional
    return TradePlan(
        side=side,
        entry_px=entry,
        stop_px=stop_px,
        target_px=target_px,
        risk_per_unit=risk_per_unit,
        risk_amount=risk_amount,
        qty=qty,
        notional=notional,
        leverage=leverage,
        margin_required=margin_required,
        leverage_note=leverage_note,
        support=support,
        resistance=resistance,
        rr=rr,
        structure_rr=structure_rr,
        stop_source=stop_source,
        blocked_reason=blocked,
    )


def _plan_summary(plan: TradePlan) -> str:
    support = f"{plan.support:,.4f}" if plan.support is not None else "n/a"
    resistance = f"{plan.resistance:,.4f}" if plan.resistance is not None else "n/a"
    structure_rr = f"{plan.structure_rr:.2f}R" if plan.structure_rr is not None else "n/a"
    return (
        f"PLAN {_fmt_side(plan.side)} | Entry={plan.entry_px:,.4f} SL={plan.stop_px:,.4f} "
        f"Target={plan.target_px:,.4f} RR=1:{plan.rr:.2f} Qty={plan.qty:,.8f} "
        f"Notional=${plan.notional:,.2f} Margin~${plan.margin_required:,.2f} Lev={plan.leverage:.2f}x "
        f"Risk=${plan.risk_amount:,.2f} Support={support} Resistance={resistance} "
        f"StructureRR={structure_rr} StopSource={plan.stop_source}"
        + (f" LevNote={plan.leverage_note}" if plan.leverage_note else "")
    )


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


def _cap_entry_qty(
    side: int,
    desired_qty: float,
    price: float,
    cfg: RuntimeConfig,
    leverage_override: float | None = None,
) -> Tuple[float, str | None]:
    if not cfg.place_orders:
        return desired_qty, None

    try:
        available_quote, quote = fetch_available_quote_balance(cfg)
    except Exception as exc:
        return 0.0, f"Could not fetch balances for live sizing: {exc}"
    if available_quote is None or quote is None:
        return 0.0, "Could not determine available quote balance for live sizing."

    if cfg.execution_mode == "margin":
        leverage = leverage_override if leverage_override is not None else _effective_leverage(cfg)
        max_margin_pct = max(_env_float("MAX_MARGIN_PCT", 1.0), 0.05)
        max_margin_pct = min(max_margin_pct, 1.0)
        effective_available = available_quote * max_margin_pct
        max_notional = effective_available * leverage
    elif cfg.execution_mode == "futures":
        instrument = fetch_futures_instrument_details(cfg.pair, cfg.futures_margin_currency)
        leverage = leverage_override if leverage_override is not None else _effective_leverage(cfg)
        max_margin_pct = max(_env_float("MAX_MARGIN_PCT", 1.0), 0.05)
        max_margin_pct = min(max_margin_pct, 1.0)
        effective_available = available_quote * max_margin_pct
        max_notional = effective_available * leverage
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
                f"capped={capped_qty:,.8f}, available={available_quote:,.4f} {quote}, leverage={leverage:.2f}x.",
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
    leverage: float | None = None,
) -> dict:
    return {
        "side": side,
        "order_type": "market_order",
        "market": market,
        "quantity": qty,
        "leverage": leverage if leverage is not None else cfg.leverage,
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
    leverage: float | None = None,
) -> Tuple[bool, str, str | None]:
    if qty <= 0:
        return False, "invalid qty <= 0", None
    qty = _round_qty(qty, cfg.qty_precision)
    if qty <= 0:
        return False, "rounded qty <= 0", None
    if not cfg.place_orders:
        return True, f"PAPER margin order side={side} qty={qty}", None

    try:
        body = _build_margin_order(
            side=side,
            market=cfg.market,
            qty=qty,
            stop_px=stop_px,
            target_px=target_px,
            cfg=cfg,
            leverage=leverage,
        )
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
    leverage: float | None = None,
) -> dict:
    order = {
        "side": side,
        "pair": cfg.pair,
        "order_type": "market_order",
        "price": None,
        "stop_price": None,
        "total_quantity": qty,
        "leverage": leverage if leverage is not None else cfg.leverage,
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
    leverage: float | None = None,
) -> Tuple[bool, str, str | None]:
    if qty <= 0:
        return False, "invalid qty <= 0", None
    qty = _round_qty(qty, cfg.qty_precision)
    if qty <= 0:
        return False, "rounded qty <= 0", None
    if not cfg.place_orders:
        return True, f"PAPER futures order side={side} qty={qty}", None
    try:
        body = _build_futures_order(side=side, qty=qty, cfg=cfg, leverage=leverage)
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
    stop_override: float | None = None,
    target_override: float | None = None,
) -> str:
    stop_px = stop_override if stop_override is not None else st_line
    risk_per_unit = _risk_per_unit(px, stop_px)
    qty = qty_override if qty_override is not None else risk_dollars / risk_per_unit
    target = target_override if target_override is not None else (px + 2 * risk_per_unit if side == 1 else px - 2 * risk_per_unit)
    notional = qty * px

    state.side = side
    state.trade_id += 1
    state.entry_ts = ts
    state.entry_px = px
    state.stop_px = stop_px
    state.target_px = target
    state.qty = qty
    state.init_risk_per_unit = risk_per_unit

    return (
        f"[{_fmt_ts(ts)}] ENTRY {_fmt_side(side)} | Trade #{state.trade_id} | "
        f"Entry={px:,.4f} SL={state.stop_px:,.4f} Target={state.target_px:,.4f} | "
        f"Qty={state.qty:,.8f} (Risk ${risk_dollars:.2f}, Notional ${notional:,.2f})"
    )


def _exit_trade(state: TradeState, ts: pd.Timestamp, exit_px: float, reason: str, risk_dollars: float, cfg: "RuntimeConfig | None" = None) -> str:
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

    _pending_db_writes.append({
        "pair": cfg.pair if cfg else "",
        "side": state.side,
        "entry_ts": state.entry_ts,
        "exit_ts": ts,
        "entry_px": state.entry_px,
        "exit_px": exit_px,
        "stop_px": state.stop_px,
        "target_px": state.target_px,
        "qty": state.qty,
        "risk_usd": risk_dollars,
        "pnl": pnl,
        "exit_reason": reason,
        "mode": cfg.execution_mode if cfg else None,
        "strategy": cfg.strategy if cfg else None,
        "execution_mode": "real" if (cfg and cfg.place_orders) else "paper",
    })

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
        _pending_db_writes.append({
            "pair": cfg.pair,
            "side": state.side,
            "entry_ts": state.entry_ts,
            "exit_ts": ts,
            "entry_px": state.entry_px,
            "exit_px": avg_exit if avg_exit > 0 else None,
            "stop_px": state.stop_px,
            "target_px": state.target_px,
            "qty": state.qty,
            "risk_usd": cfg.risk_dollars,
            "pnl": broker_pnl,
            "exit_reason": status.upper(),
            "mode": cfg.execution_mode,
            "strategy": cfg.strategy if cfg else None,
            "execution_mode": "real",
        })
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
            _pending_db_writes.append({
                "pair": cfg.pair,
                "side": state.side,
                "entry_ts": state.entry_ts,
                "exit_ts": ts,
                "entry_px": state.entry_px,
                "exit_px": None,
                "stop_px": state.stop_px,
                "target_px": state.target_px,
                "qty": state.qty,
                "risk_usd": cfg.risk_dollars,
                "pnl": None,
                "exit_reason": "BROKER_CLOSE",
                "mode": cfg.execution_mode,
                "strategy": cfg.strategy if cfg else None,
                "execution_mode": "real" if cfg.place_orders else "paper",
            })
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


def _row_score(row: pd.Series) -> int:
    value = row.get("score", 0)
    try:
        value = 0.0 if pd.isna(value) else float(value)
    except (TypeError, ValueError):
        return 0
    if not np.isfinite(value):
        return 0
    return int(value)


def _row_bool(row: pd.Series, key: str) -> bool:
    value = row.get(key, False)
    if pd.isna(value):
        return False
    return bool(value)


def _row_entry_side(row: pd.Series, score: int) -> int:
    value = row.get("entry_side", 0)
    try:
        side = 0 if pd.isna(value) else int(float(value))
    except (TypeError, ValueError):
        side = 0
    if side in (1, -1):
        return side
    if "entry_side" in row.index:
        return 0
    if score >= LONG_ENTRY_SCORE:
        return 1
    if score <= SHORT_ENTRY_SCORE:
        return -1
    return 0


def _row_should_exit(row: pd.Series, side: int, score: int) -> bool:
    if side == 1:
        if "exit_long" in row.index:
            return _row_bool(row, "exit_long")
        return score < LONG_EXIT_SCORE
    if side == -1:
        if "exit_short" in row.index:
            return _row_bool(row, "exit_short")
        return score > SHORT_EXIT_SCORE
    return False


def process_closed_bar_futures(
    ts: pd.Timestamp,
    row: pd.Series,
    state: TradeState,
    cfg: RuntimeConfig,
    history: pd.DataFrame | None = None,
) -> List[str]:
    score = _row_score(row)
    close = float(row["Close"])
    high = float(row["High"])
    low = float(row["Low"])
    st_line = float(row["st_line"])
    events: List[str] = []

    if not cfg.place_orders:
        return process_closed_bar(ts, row, state, RuntimeConfig(**{**cfg.__dict__, "execution_mode": "spot"}), history)

    if state.side == 0:
        signal_side = _row_entry_side(row, score)
        if signal_side == -1 and not cfg.allow_shorts:
            events.append(f"[{_fmt_ts(ts)}] SHORT SIGNAL score={score:+d} ignored (futures shorts disabled).")
            return events
        if signal_side not in (1, -1):
            return events

        plan = _build_trade_plan(signal_side, ts, row, history, cfg)
        events.append(f"[{_fmt_ts(ts)}] {_plan_summary(plan)}")
        if plan.blocked_reason:
            events.append(
                f"[{_fmt_ts(ts)}] {_fmt_side(signal_side)} ENTRY BLOCKED | "
                f"{plan.blocked_reason}"
            )
            return events

        entry_qty, cap_msg = _cap_entry_qty(signal_side, plan.qty, close, cfg, plan.leverage)
        if entry_qty <= 0:
            events.append(
                f"[{_fmt_ts(ts)}] {_fmt_side(signal_side)} ENTRY BLOCKED | {cap_msg or 'qty <= 0'}"
            )
            return events
        if abs(entry_qty - plan.qty) > 1e-12:
            plan.qty = entry_qty
            plan.notional = plan.qty * plan.entry_px
            plan.margin_required = plan.notional / plan.leverage if plan.leverage > 0 else plan.notional

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
        enter_msg = _enter_trade(
            preview,
            signal_side,
            ts,
            close,
            st_line,
            plan.risk_amount,
            qty_override=entry_qty,
            stop_override=plan.stop_px,
            target_override=plan.target_px,
        )
        order_side = "buy" if signal_side == 1 else "sell"
        ok, broker_msg, broker_order_id = _place_futures_order(order_side, preview.qty, cfg, plan.leverage)
        if not ok:
            events.append(f"[{_fmt_ts(ts)}] {_fmt_side(signal_side)} ENTRY BLOCKED | {broker_msg}")
            return events
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
        planned_stop = plan.stop_px
        if state.side == 1 and planned_stop >= state.entry_px:
            live_brackets = _derive_brackets(state.side, state.entry_px, st_line)
        elif state.side == -1 and planned_stop <= state.entry_px:
            live_brackets = _derive_brackets(state.side, state.entry_px, st_line)
        else:
            live_target = _target_from_stop(state.side, state.entry_px, planned_stop, plan.rr)
            live_brackets = (planned_stop, live_target, _risk_per_unit(state.entry_px, planned_stop))
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

    should_exit = "SIGNAL" if _row_should_exit(row, state.side, score) else None

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


def process_closed_bar(
    ts: pd.Timestamp,
    row: pd.Series,
    state: TradeState,
    cfg: RuntimeConfig,
    history: pd.DataFrame | None = None,
) -> List[str]:
    if cfg.execution_mode == "futures":
        return process_closed_bar_futures(ts, row, state, cfg, history)

    score = _row_score(row)
    close = float(row["Close"])
    high = float(row["High"])
    low = float(row["Low"])
    st_line = float(row["st_line"])
    events: List[str] = []

    if state.side == 0:
        signal_side = _row_entry_side(row, score)
        if signal_side == 1:
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
            if cfg.execution_mode == "margin":
                plan = _build_trade_plan(1, ts, row, history, cfg)
                events.append(f"[{_fmt_ts(ts)}] {_plan_summary(plan)}")
                if plan.blocked_reason:
                    events.append(
                        f"[{_fmt_ts(ts)}] LONG ENTRY BLOCKED | {plan.blocked_reason}"
                    )
                    return events
                entry_qty, cap_msg = _cap_entry_qty(1, plan.qty, close, cfg, plan.leverage)
                risk_amount = plan.risk_amount
                stop_override = plan.stop_px
                target_override = plan.target_px
                leverage_override = plan.leverage
            else:
                desired_qty = _desired_qty(close, st_line, cfg.risk_dollars)
                entry_qty, cap_msg = _cap_entry_qty(1, desired_qty, close, cfg)
                risk_amount = cfg.risk_dollars
                stop_override = None
                target_override = None
                leverage_override = None
            if entry_qty <= 0:
                events.append(f"[{_fmt_ts(ts)}] LONG ENTRY BLOCKED | {cap_msg or 'qty <= 0'}")
                return events
            enter_msg = _enter_trade(
                preview,
                1,
                ts,
                close,
                st_line,
                risk_amount,
                qty_override=entry_qty,
                stop_override=stop_override,
                target_override=target_override,
            )
            if cfg.execution_mode == "margin":
                ok, broker_msg, broker_order_id = _place_margin_order(
                    "buy",
                    preview.qty,
                    preview.stop_px,
                    preview.target_px,
                    cfg,
                    leverage_override,
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
        elif signal_side == -1:
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
            if cfg.execution_mode == "margin":
                plan = _build_trade_plan(-1, ts, row, history, cfg)
                events.append(f"[{_fmt_ts(ts)}] {_plan_summary(plan)}")
                if plan.blocked_reason:
                    events.append(
                        f"[{_fmt_ts(ts)}] SHORT ENTRY BLOCKED | {plan.blocked_reason}"
                    )
                    return events
                entry_qty, cap_msg = _cap_entry_qty(-1, plan.qty, close, cfg, plan.leverage)
                risk_amount = plan.risk_amount
                stop_override = plan.stop_px
                target_override = plan.target_px
                leverage_override = plan.leverage
            else:
                desired_qty = _desired_qty(close, st_line, cfg.risk_dollars)
                entry_qty, cap_msg = _cap_entry_qty(-1, desired_qty, close, cfg)
                risk_amount = cfg.risk_dollars
                stop_override = None
                target_override = None
                leverage_override = None
            if entry_qty <= 0:
                events.append(f"[{_fmt_ts(ts)}] SHORT ENTRY BLOCKED | {cap_msg or 'qty <= 0'}")
                return events
            enter_msg = _enter_trade(
                preview,
                -1,
                ts,
                close,
                st_line,
                risk_amount,
                qty_override=entry_qty,
                stop_override=stop_override,
                target_override=target_override,
            )
            if cfg.execution_mode == "margin":
                ok, broker_msg, broker_order_id = _place_margin_order(
                    "sell",
                    preview.qty,
                    preview.stop_px,
                    preview.target_px,
                    cfg,
                    leverage_override,
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
        elif _row_should_exit(row, 1, score):
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
            events.append(_exit_trade(state, ts, exit_px, should_exit, cfg.risk_dollars, cfg))
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
        elif _row_should_exit(row, -1, score):
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
            events.append(_exit_trade(state, ts, exit_px, should_exit, cfg.risk_dollars, cfg))
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
        f"Live Confluence Monitor | Pair={cfg.pair} | Market={cfg.market} | TF={cfg.timeframe} | "
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
        print(
            f"Margin execution enabled. BaseLeverage={cfg.leverage:.2f} "
            f"MaxLeverage={_env_float('MAX_LEVERAGE', cfg.leverage):.2f} Ecode={cfg.margin_ecode}"
        )
    if cfg.execution_mode == "futures":
        print(
            f"Futures execution enabled. MarginCurrency={cfg.futures_margin_currency} "
            f"MarginType={cfg.position_margin_type} BaseLeverage={cfg.leverage:.2f} "
            f"MaxLeverage={_env_float('MAX_LEVERAGE', cfg.leverage):.2f}"
        )

    while True:
        try:
            bars, latest_closed, used_pair, used_source = fetch_closed_bars(
                cfg.pair,
                cfg.market,
                lookback_days=cfg.lookback_days,
                execution_mode=cfg.execution_mode,
                timeframe=cfg.timeframe,
            )
            if used_pair and (used_pair != cfg.candle_pair or used_source != cfg.data_source):
                print(f"Data feed selected: pair={used_pair} source={used_source}")
                cfg.candle_pair = used_pair
                cfg.data_source = used_source
            if used_pair:
                cached_bars = load_cached_bars(used_pair, cfg.execution_mode, timeframe=cfg.timeframe)
                bars = merge_bars(cached_bars, bars)
                bars = bars[bars.index <= latest_closed]
                save_cached_bars(used_pair, cfg.execution_mode, bars, timeframe=cfg.timeframe)
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
            _, _, bar_minutes = _normalize_timeframe(cfg.timeframe)
            if latest_closed - last_bar_ts > pd.Timedelta(minutes=bar_minutes * 6):
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
                    process_closed_bar(ts, row, state, boot_cfg, frame.loc[:ts])
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
                    startup_events = process_closed_bar(last_processed_ts, last_row, state, cfg, frame.loc[:last_processed_ts])
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
                    events = process_closed_bar(ts, row, state, cfg, frame.loc[:ts])
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
        description="CoinDCX live confluence monitor with fixed-dollar risk sizing"
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
    parser.add_argument(
        "--max-leverage",
        type=float,
        default=None,
        help="Safety cap for live leverage (or use MAX_LEVERAGE env var)",
    )
    parser.add_argument(
        "--risk-reward",
        type=float,
        default=None,
        help="Risk:reward target for planned TP/SL (or use RISK_REWARD_RATIO env var)",
    )
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
    parser.add_argument(
        "--timeframe",
        type=str,
        default=TIMEFRAME,
        help="Candle timeframe (15m)",
    )
    parser.add_argument("--api-key", type=str, default=None, help="API key (or use COINDCX_API_KEY env var)")
    parser.add_argument("--api-secret", type=str, default=None, help="API secret (or use COINDCX_API_SECRET env var)")
    args = parser.parse_args()

    market = args.market or derive_market_from_pair(args.pair)
    api_key = args.api_key or os.getenv("COINDCX_API_KEY")
    api_secret = args.api_secret or os.getenv("COINDCX_API_SECRET")
    place_orders = (
        bool(args.place_orders)
        or _env_bool("PLACE_ORDERS")
        or _env_bool("COINDCX_PLACE_ORDERS")
        or _env_bool("BOT_PLACE_ORDERS")
    )
    execution_mode = args.execution_mode
    if execution_mode == "auto":
        execution_mode = "futures" if place_orders else "spot"
    max_leverage = args.max_leverage if args.max_leverage is not None else _env_float("MAX_LEVERAGE", float(args.leverage))
    leverage = min(float(args.leverage), max(max_leverage, 1.0))
    if args.risk_reward is not None:
        os.environ["RISK_REWARD_RATIO"] = str(max(float(args.risk_reward), 1.0))

    timeframe, _, _ = _normalize_timeframe(args.timeframe)
    cfg = RuntimeConfig(
        pair=args.pair,
        market=market,
        risk_dollars=float(args.risk),
        poll_seconds=int(args.poll),
        place_orders=place_orders,
        allow_shorts=bool(args.allow_shorts),
        execution_mode=str(execution_mode),
        leverage=leverage,
        margin_ecode=str(args.margin_ecode).upper(),
        futures_margin_currency=str(args.futures_margin_currency).upper(),
        position_margin_type=str(args.position_margin_type).lower(),
        qty_precision=int(args.qty_precision),
        lookback_days=int(args.lookback_days),
        timeframe=timeframe,
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
