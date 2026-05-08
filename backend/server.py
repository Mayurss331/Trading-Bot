#!/usr/bin/env python3
"""
CoinDCX bot dashboard backend.

Runs a small standard-library HTTP server that serves the dashboard frontend and
JSON APIs backed by Crypto/live_confluence_monitor.py.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import traceback
import importlib.util
from concurrent.futures import ThreadPoolExecutor, as_completed
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ROOT / "frontend"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "dashboard.sqlite3"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BOT_PATH = ROOT / "Crypto" / "live_confluence_monitor.py"
BOT_SPEC = importlib.util.spec_from_file_location("live_confluence_monitor", BOT_PATH)
if BOT_SPEC is None or BOT_SPEC.loader is None:
    raise RuntimeError(f"Could not load bot module from {BOT_PATH}")
bot = importlib.util.module_from_spec(BOT_SPEC)
sys.modules[BOT_SPEC.name] = bot
BOT_SPEC.loader.exec_module(bot)

from strategies.base import StrategyContext  # noqa: E402
from strategies.registry import get_strategy, list_strategies, normalize_strategy_id  # noqa: E402


def load_env_file(path: Path) -> None:
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


load_env_file(ROOT / ".env")

DEFAULT_PAIR = os.getenv("DEFAULT_PAIR", "B-ETH_USDT")
DEFAULT_MARKET = os.getenv("DEFAULT_MARKET", "ETHUSDT")
DEFAULT_FUTURES_COINS = [
    "BTC",
    "ETH",
    "SOL",
    "XRP",
    "BNB",
    "DOGE",
    "ADA",
    "LINK",
    "AVAX",
    "DOT",
    "LTC",
    "TRX",
]
FUTURES_CATALOG_CACHE: dict[str, object] = {"ts": 0.0, "coins": []}
FUTURES_CATALOG_TTL_SECONDS = 600
DEFAULT_SETTINGS = {
    "pair": DEFAULT_PAIR,
    "market": DEFAULT_MARKET,
    "mode": os.getenv("DEFAULT_MODE", "futures"),
    "strategy": os.getenv("DEFAULT_STRATEGY", "confluence"),
    "risk": os.getenv("DEFAULT_RISK", "10"),
    "reward_ratio": os.getenv("DEFAULT_REWARD_RATIO", "2"),
    "leverage": os.getenv("DEFAULT_LEVERAGE", "1"),
    "lookback_days": os.getenv("DEFAULT_LOOKBACK_DAYS", "3"),
    "selected_coins": "BTC,ETH,SOL",
}


def db_conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dashboard_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    return conn


def load_settings() -> dict[str, str]:
    settings = DEFAULT_SETTINGS.copy()
    with db_conn() as conn:
        rows = conn.execute("SELECT key, value FROM dashboard_settings").fetchall()
    for key, value in rows:
        if key in settings:
            settings[key] = value
    return settings


def save_settings(values: dict[str, object]) -> dict[str, str]:
    allowed = set(DEFAULT_SETTINGS)
    clean: dict[str, str] = {}
    for key, value in values.items():
        if key not in allowed or value is None:
            continue
        if key == "selected_coins" and isinstance(value, list):
            value = ",".join(str(item).upper() for item in value if str(item).strip())
        clean[key] = str(value)

    if clean:
        with db_conn() as conn:
            conn.executemany(
                """
                INSERT INTO dashboard_settings(key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    updated_at=CURRENT_TIMESTAMP
                """,
                list(clean.items()),
            )
    return load_settings()


def futures_pair_for_coin(coin: str) -> str:
    clean = "".join(ch for ch in coin.upper().strip() if ch.isalnum())
    if not clean:
        clean = "ETH"
    return f"B-{clean}_USDT"


def futures_market_for_coin(coin: str) -> str:
    clean = "".join(ch for ch in coin.upper().strip() if ch.isalnum())
    if not clean:
        clean = "ETH"
    return f"{clean}USDT"


def coin_from_pair(pair: str) -> str:
    suffix = pair.split("-", 1)[1] if "-" in pair else pair
    return suffix.split("_", 1)[0].upper()


def _fallback_futures_catalog() -> list[dict]:
    return [
        {
            "coin": coin,
            "pair": futures_pair_for_coin(coin),
            "market": futures_market_for_coin(coin),
            "status": "fallback",
            "max_leverage": None,
            "min_notional": None,
        }
        for coin in DEFAULT_FUTURES_COINS
    ]


def _candidate_futures_pairs() -> list[str]:
    default_pairs = [futures_pair_for_coin(coin) for coin in DEFAULT_FUTURES_COINS]
    try:
        markets = bot.fetch_markets_details()
    except Exception:
        return default_pairs

    pairs: list[str] = default_pairs.copy()
    for row in markets:
        pair = str(row.get("pair") or "")
        status = str(row.get("status") or "").lower()
        if status != "active":
            continue
        if not pair.startswith("B-") or not pair.endswith("_USDT"):
            continue
        if pair not in pairs:
            pairs.append(pair)
    return pairs


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
        )
        or None,
        "min_notional": instrument.get("min_notional"),
        "price_increment": instrument.get("price_increment"),
        "quantity_increment": instrument.get("quantity_increment"),
    }


def fetch_futures_catalog(force: bool = False) -> list[dict]:
    now = time.time()
    cached = FUTURES_CATALOG_CACHE.get("coins")
    if (
        not force
        and isinstance(cached, list)
        and cached
        and now - float(FUTURES_CATALOG_CACHE.get("ts") or 0) < FUTURES_CATALOG_TTL_SECONDS
    ):
        return cached

    pairs = _candidate_futures_pairs()
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = {executor.submit(_fetch_active_futures_instrument, pair): pair for pair in pairs}
        for future in as_completed(futures):
            row = future.result()
            if row:
                rows.append(row)

    popular_rank = {coin: idx for idx, coin in enumerate(DEFAULT_FUTURES_COINS)}
    rows = sorted(rows, key=lambda row: (popular_rank.get(str(row["coin"]), 999), str(row["coin"])))
    if not rows:
        rows = _fallback_futures_catalog()

    FUTURES_CATALOG_CACHE["ts"] = now
    FUTURES_CATALOG_CACHE["coins"] = rows
    return rows


def _param(query: dict[str, list[str]], name: str, default: str) -> str:
    value = query.get(name, [default])[0]
    return value.strip() if isinstance(value, str) and value.strip() else default


def _int_param(query: dict[str, list[str]], name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(query.get(name, [str(default)])[0])
    except Exception:
        value = default
    return min(max(value, low), high)


def _float_param(query: dict[str, list[str]], name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(query.get(name, [str(default)])[0])
    except Exception:
        value = default
    return min(max(value, low), high)


def _clean(value):
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        if not np.isfinite(value):
            return None
        return float(value)
    if isinstance(value, float) and not np.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _cfg(pair: str, market: str, mode: str, risk: float, lookback_days: int) -> bot.RuntimeConfig:
    return bot.RuntimeConfig(
        pair=pair,
        market=market,
        risk_dollars=risk,
        poll_seconds=15,
        place_orders=os.getenv("PLACE_ORDERS", "false").lower() == "true",
        allow_shorts=mode in {"margin", "futures"},
        execution_mode=mode,
        leverage=1.0,
        margin_ecode="B",
        futures_margin_currency="USDT",
        position_margin_type="crossed",
        qty_precision=6,
        lookback_days=lookback_days,
        api_key=os.getenv("COINDCX_API_KEY"),
        api_secret=os.getenv("COINDCX_API_SECRET"),
    )


def _state_payload(state: bot.TradeState) -> dict:
    return {
        "side": bot._fmt_side(state.side),
        "trade_id": state.trade_id,
        "entry_ts": state.entry_ts,
        "entry_px": state.entry_px,
        "stop_px": state.stop_px,
        "target_px": state.target_px,
        "qty": state.qty,
        "realized_pnl": state.realized_pnl,
        "broker_order_status": state.broker_order_status,
        "exit_pending": state.exit_pending,
        "position_id": state.position_id,
    }


def _latest_action(score: int, state: bot.TradeState, allow_shorts: bool) -> dict:
    if state.side == 0:
        if score >= bot.LONG_ENTRY_SCORE:
            return {"type": "ENTRY", "side": "LONG", "label": "Long entry setup"}
        if score <= bot.SHORT_ENTRY_SCORE:
            if allow_shorts:
                return {"type": "ENTRY", "side": "SHORT", "label": "Short entry setup"}
            return {"type": "IGNORE", "side": "SHORT", "label": "Short signal ignored in spot mode"}
        return {"type": "WAIT", "side": "FLAT", "label": "No entry setup"}
    if state.side == 1 and score < bot.LONG_EXIT_SCORE:
        return {"type": "EXIT", "side": "LONG", "label": "Long exit setup"}
    if state.side == -1 and score > bot.SHORT_EXIT_SCORE:
        return {"type": "EXIT", "side": "SHORT", "label": "Short exit setup"}
    return {"type": "HOLD", "side": bot._fmt_side(state.side), "label": "Position still valid"}


def _bars_payload(bars: pd.DataFrame, frame: pd.DataFrame, limit: int) -> list[dict]:
    limited = bars.tail(limit)
    frame_limited = frame.reindex(limited.index)
    rows: list[dict] = []
    for ts, row in limited.iterrows():
        ind = frame_limited.loc[ts] if ts in frame_limited.index else None
        rows.append(
            {
                "time": ts,
                "open": row.get("Open"),
                "high": row.get("High"),
                "low": row.get("Low"),
                "close": row.get("Close"),
                "volume": row.get("Volume"),
                "score": None if ind is None or pd.isna(ind.get("score")) else int(ind.get("score")),
                "raw_score": None if ind is None or pd.isna(ind.get("score")) else float(ind.get("score")),
                "rsi": None if ind is None or pd.isna(ind.get("rsi")) else float(ind.get("rsi")),
                "supertrend": None if ind is None or pd.isna(ind.get("st_line")) else float(ind.get("st_line")),
                "ema_fast": None if ind is None or pd.isna(ind.get("ema_fast")) else float(ind.get("ema_fast")),
                "ema_slow": None if ind is None or pd.isna(ind.get("ema_slow")) else float(ind.get("ema_slow")),
                "bb_lower": None if ind is None or pd.isna(ind.get("bb_lower")) else float(ind.get("bb_lower")),
                "bb_mid": None if ind is None or pd.isna(ind.get("bb_mid")) else float(ind.get("bb_mid")),
                "bb_upper": None if ind is None or pd.isna(ind.get("bb_upper")) else float(ind.get("bb_upper")),
            }
        )
    return rows


def build_snapshot(query: dict[str, list[str]]) -> dict:
    pair = _param(query, "pair", DEFAULT_PAIR)
    market = _param(query, "market", bot.derive_market_from_pair(pair) or DEFAULT_MARKET)
    strategy_id = normalize_strategy_id(_param(query, "strategy", "confluence"))
    mode = _param(query, "mode", "spot").lower()
    if mode not in {"spot", "margin", "futures"}:
        mode = "spot"
    lookback_days = _int_param(query, "lookback_days", 3, 1, 30)
    limit = _int_param(query, "limit", 240, 60, 1000)
    risk = _float_param(query, "risk", 10.0, 0.01, 1_000_000.0)
    reward_ratio = _float_param(query, "reward_ratio", 2.0, 0.1, 20.0)
    leverage = _float_param(query, "leverage", 1.0, 0.1, 125.0)

    cfg = _cfg(pair, market, mode, risk, lookback_days)
    bars, latest_closed, used_pair, used_source = bot.fetch_closed_bars(
        cfg.pair,
        cfg.market,
        lookback_days=cfg.lookback_days,
        execution_mode=cfg.execution_mode,
    )
    if bars.empty:
        return {
            "ok": False,
            "message": "No CoinDCX bars returned for this pair/market.",
            "pair": pair,
            "market": market,
            "mode": mode,
            "bars": [],
        }

    spot_ticker = None
    futures_ticker = None
    try:
        if mode != "futures" or strategy_id == "arbitrage":
            spot_ticker = bot.fetch_market_ticker(market)
    except Exception:
        spot_ticker = None
    try:
        if mode == "futures" or strategy_id == "arbitrage":
            futures_ticker = bot.fetch_futures_ticker(pair)
    except Exception:
        futures_ticker = None

    strategy_ctx = StrategyContext(
        pair=pair,
        market=market,
        mode=mode,
        risk=risk,
        reward_ratio=reward_ratio,
        leverage=leverage,
        allow_shorts=cfg.allow_shorts,
        extras={"spot_ticker": spot_ticker, "futures_ticker": futures_ticker},
    )
    analysis = get_strategy(strategy_id)(bars, strategy_ctx)
    frame = analysis["frame"]
    meta = analysis["meta"]
    ticker = futures_ticker if mode == "futures" else spot_ticker

    last_bar = bars.iloc[-1]
    first_close = float(bars["Close"].iloc[0])
    last_close = float(last_bar["Close"])
    latest_frame = frame.iloc[-1] if not frame.empty else None
    score = float(latest_frame["score"]) if latest_frame is not None else 0
    freshness = bot._freshness_minutes(bars.index.max())

    return {
        "ok": True,
        "coin": coin_from_pair(pair),
        "pair": pair,
        "market": market,
        "mode": mode,
        "risk_model": {
            "risk": risk,
            "reward_ratio": reward_ratio,
            "leverage": leverage,
        },
        "strategy": {
            "id": meta.id,
            "name": meta.name,
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
        "bars": _bars_payload(bars, frame, limit),
    }


def build_strategies() -> dict:
    return {"ok": True, "strategies": list_strategies()}


def build_futures_markets() -> dict:
    coins = fetch_futures_catalog()
    return {
        "ok": True,
        "source": "coindcx_futures_instruments" if coins and coins[0].get("status") != "fallback" else "fallback",
        "count": len(coins),
        "coins": coins,
    }


def _track_row_from_snapshot(snapshot: dict) -> dict:
    strategy = snapshot.get("strategy") or {}
    stats = snapshot.get("stats") or {}
    action = snapshot.get("action") or {}
    state = snapshot.get("state") or {}
    ticker = snapshot.get("ticker") or {}
    return {
        "ok": bool(snapshot.get("ok")),
        "coin": snapshot.get("coin") or coin_from_pair(str(snapshot.get("pair") or "")),
        "pair": snapshot.get("pair"),
        "market": snapshot.get("market"),
        "strategy": strategy.get("id"),
        "strategy_name": strategy.get("name"),
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
        "qty": state.get("qty"),
        "entry": state.get("entry_px"),
        "stop": state.get("stop_px"),
        "target": state.get("target_px"),
        "notional": state.get("notional"),
        "margin_required": state.get("margin_required"),
        "reward_ratio": state.get("reward_ratio"),
        "freshness_minutes": snapshot.get("freshness_minutes"),
        "latest_time": stats.get("latest_time"),
        "message": snapshot.get("message"),
    }


def build_track(query: dict[str, list[str]]) -> dict:
    raw_coins = _param(query, "coins", "BTC,ETH,SOL")
    strategy_id = normalize_strategy_id(_param(query, "strategy", "confluence"))
    risk = _float_param(query, "risk", 10.0, 0.01, 1_000_000.0)
    reward_ratio = _float_param(query, "reward_ratio", 2.0, 0.1, 20.0)
    leverage = _float_param(query, "leverage", 1.0, 0.1, 125.0)
    lookback_days = _int_param(query, "lookback_days", 2, 1, 14)
    coins = []
    for item in raw_coins.replace(" ", "").split(","):
        if not item:
            continue
        coin = coin_from_pair(item) if "_" in item or "-" in item else item.upper()
        if coin not in coins:
            coins.append(coin)
    coins = coins[:12]

    rows = []
    for coin in coins:
        pair = futures_pair_for_coin(coin)
        market = futures_market_for_coin(coin)
        try:
            snapshot = build_snapshot(
                {
                    "pair": [pair],
                    "market": [market],
                    "mode": ["futures"],
                    "strategy": [strategy_id],
                    "risk": [str(risk)],
                    "reward_ratio": [str(reward_ratio)],
                    "leverage": [str(leverage)],
                    "lookback_days": [str(lookback_days)],
                    "limit": ["80"],
                }
            )
            rows.append(_track_row_from_snapshot(snapshot))
        except Exception as exc:
            rows.append(
                {
                    "ok": False,
                    "coin": coin,
                    "pair": pair,
                    "market": market,
                    "strategy": strategy_id,
                    "message": str(exc),
                }
            )
    return {
        "ok": True,
        "mode": "futures",
        "strategy": strategy_id,
        "tracked": rows,
        "server_time": pd.Timestamp.now(tz=bot.IST),
    }


def build_markets(query: dict[str, list[str]]) -> dict:
    search = _param(query, "q", "").upper()
    limit = _int_param(query, "limit", 80, 1, 300)
    markets = bot.fetch_markets_details()
    rows = []
    for row in markets:
        pair = str(row.get("pair") or "")
        symbol = str(row.get("symbol") or "")
        status = str(row.get("status") or "")
        text = f"{pair} {symbol}".upper()
        if search and search not in text:
            continue
        rows.append(
            {
                "pair": pair,
                "market": symbol,
                "ecode": row.get("ecode"),
                "status": status,
                "target_currency": row.get("target_currency_short_name"),
                "base_currency": row.get("base_currency_short_name"),
            }
        )
        if len(rows) >= limit:
            break
    return {"ok": True, "markets": rows}


def build_account(query: dict[str, list[str]]) -> dict:
    pair = _param(query, "pair", DEFAULT_PAIR)
    market = _param(query, "market", bot.derive_market_from_pair(pair) or DEFAULT_MARKET)
    mode = _param(query, "mode", "futures").lower()
    cfg = _cfg(pair, market, mode, 10.0, 1)
    has_credentials = bool(cfg.api_key and cfg.api_secret)
    if not has_credentials:
        return {
            "ok": True,
            "has_credentials": False,
            "mode": mode,
            "message": "COINDCX_API_KEY/SECRET are not set.",
            "risk_suggestions": [],
        }
    available, currency = bot.fetch_available_quote_balance(cfg)
    available = float(available or 0.0)
    suggestions = [
        {"label": "0.25%", "percent": 0.25, "amount": available * 0.0025},
        {"label": "0.5%", "percent": 0.5, "amount": available * 0.005},
        {"label": "1%", "percent": 1.0, "amount": available * 0.01},
        {"label": "2%", "percent": 2.0, "amount": available * 0.02},
    ]
    wallets = []
    positions = []
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
                    positions.append(
                        {
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
                        }
                    )
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


def build_live_quote(query: dict[str, list[str]]) -> dict:
    pair = _param(query, "pair", DEFAULT_PAIR)
    market = _param(query, "market", bot.derive_market_from_pair(pair) or DEFAULT_MARKET)
    mode = _param(query, "mode", "futures").lower()
    if mode not in {"spot", "margin", "futures"}:
        mode = "futures"

    ticker = bot.fetch_futures_ticker(pair) if mode == "futures" else bot.fetch_market_ticker(market)
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
            bid = None
        try:
            ask = float(ticker.get("ask"))
        except Exception:
            ask = None

    return {
        "ok": bool(ticker),
        "pair": pair,
        "market": market,
        "mode": mode,
        "server_time": pd.Timestamp.now(tz=bot.IST),
        "ticker": ticker,
        "last_price": last_price,
        "bid": bid,
        "ask": ask,
    }


def build_settings() -> dict:
    settings = load_settings()
    settings["selected_coins"] = [
        item for item in str(settings.get("selected_coins") or "").split(",") if item
    ]
    return {"ok": True, "settings": settings}


def update_settings(payload: dict) -> dict:
    settings = save_settings(payload)
    settings["selected_coins"] = [
        item for item in str(settings.get("selected_coins") or "").split(",") if item
    ]
    return {"ok": True, "settings": settings}


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FRONTEND_DIR), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        print(f"[dashboard] {self.address_string()} - {fmt % args}")

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(_clean(payload), separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self, query: dict[str, list[str]]) -> None:
        interval = _float_param(query, "interval", 1.0, 1.0, 10.0)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        while True:
            try:
                payload = build_live_quote(query)
                body = json.dumps(_clean(payload), separators=(",", ":"))
                self.wfile.write(f"event: quote\ndata: {body}\n\n".encode("utf-8"))
                self.wfile.flush()
                time.sleep(interval)
            except (BrokenPipeError, ConnectionResetError):
                break

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/health":
                self._send_json({"ok": True, "service": "coindcx-dashboard"})
            elif parsed.path == "/api/snapshot":
                self._send_json(build_snapshot(query))
            elif parsed.path == "/api/strategies":
                self._send_json(build_strategies())
            elif parsed.path == "/api/futures-markets":
                self._send_json(build_futures_markets())
            elif parsed.path == "/api/track":
                self._send_json(build_track(query))
            elif parsed.path == "/api/markets":
                self._send_json(build_markets(query))
            elif parsed.path == "/api/account":
                self._send_json(build_account(query))
            elif parsed.path == "/api/settings":
                self._send_json(build_settings())
            elif parsed.path == "/api/live":
                self._send_sse(query)
            else:
                if parsed.path == "/":
                    self.path = "/index.html"
                super().do_GET()
        except Exception as exc:
            traceback.print_exc()
            self._send_json(
                {
                    "ok": False,
                    "error": str(exc),
                    "type": exc.__class__.__name__,
                },
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/settings":
                self._send_json(update_settings(self._read_json_body()))
            else:
                self._send_json({"ok": False, "error": "not_found"}, status=HTTPStatus.NOT_FOUND)
        except Exception as exc:
            traceback.print_exc()
            self._send_json(
                {"ok": False, "error": str(exc), "type": exc.__class__.__name__},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address) -> None:
        exc_type, exc, _tb = sys.exc_info()
        if exc_type in {ConnectionResetError, BrokenPipeError, ConnectionAbortedError}:
            print(f"[dashboard] {client_address[0]} disconnected")
            return
        super().handle_error(request, client_address)


def main() -> None:
    host = os.getenv("BOT_DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("BOT_DASHBOARD_PORT", "8000"))
    server = DashboardServer((host, port), DashboardHandler)
    print(f"CoinDCX dashboard running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    server.serve_forever()


if __name__ == "__main__":
    main()
