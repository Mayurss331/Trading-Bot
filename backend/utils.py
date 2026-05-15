"""Shared utilities: serialisation, config helpers, payload builders."""
from __future__ import annotations

import os
from datetime import date, datetime

import numpy as np
import pandas as pd

from .bot_loader import bot


DEFAULT_PAIR = os.getenv("DEFAULT_PAIR", "B-ETH_USDT")
DEFAULT_MARKET = os.getenv("DEFAULT_MARKET", "ETHUSDT")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# JSON serialisation helpers
# ---------------------------------------------------------------------------

def clean(value: object) -> object:
    """Recursively replace pandas/numpy types with JSON-serialisable equivalents."""
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return None if not np.isfinite(float(value)) else float(value)
    if isinstance(value, float) and not np.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Pair / market helpers
# ---------------------------------------------------------------------------

def coin_from_pair(pair: str) -> str:
    suffix = pair.split("-", 1)[1] if "-" in pair else pair
    return suffix.split("_", 1)[0].upper()


def futures_pair_for_coin(coin: str) -> str:
    clean_coin = "".join(ch for ch in coin.upper().strip() if ch.isalnum()) or "ETH"
    return f"B-{clean_coin}_USDT"


def futures_market_for_coin(coin: str) -> str:
    clean_coin = "".join(ch for ch in coin.upper().strip() if ch.isalnum()) or "ETH"
    return f"{clean_coin}USDT"


# ---------------------------------------------------------------------------
# Query parameter helpers
# ---------------------------------------------------------------------------

def str_param(query: dict, name: str, default: str) -> str:
    value = query.get(name, default)
    if isinstance(value, list):
        value = value[0] if value else default
    value = str(value).strip()
    return value if value else default


def int_param(query: dict, name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(query.get(name, default))
    except (TypeError, ValueError):
        value = default
    return min(max(value, low), high)


def float_param(query: dict, name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(query.get(name, default))
    except (TypeError, ValueError):
        value = default
    return min(max(value, low), high)


# ---------------------------------------------------------------------------
# Bot config + payload helpers
# ---------------------------------------------------------------------------

def make_cfg(pair: str, market: str, mode: str, risk: float, lookback_days: int, timeframe: str | None = None, exec_mode: str = "") -> object:
    tf, _, _ = bot._normalize_timeframe(timeframe)
    _env_place = env_bool("PLACE_ORDERS") or env_bool("COINDCX_PLACE_ORDERS") or env_bool("BOT_PLACE_ORDERS")
    place_orders = False if exec_mode == "paper" else _env_place
    return bot.RuntimeConfig(
        pair=pair,
        market=market,
        risk_dollars=risk,
        poll_seconds=15,
        place_orders=place_orders,
        allow_shorts=mode in {"margin", "futures"},
        execution_mode=mode,
        leverage=max(1.0, min(env_float("LEVERAGE", 1.0), env_float("MAX_LEVERAGE", env_float("LEVERAGE", 1.0)))),
        margin_ecode="B",
        futures_margin_currency="USDT",
        position_margin_type="crossed",
        qty_precision=6,
        lookback_days=lookback_days,
        timeframe=tf,
        api_key=os.getenv("COINDCX_API_KEY"),
        api_secret=os.getenv("COINDCX_API_SECRET"),
    )


def state_payload(state: object) -> dict:
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


def latest_action(score: float, state: object, allow_shorts: bool) -> dict:
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


def bars_payload(bars: pd.DataFrame, frame: pd.DataFrame, limit: int, extra_cols: list[str] | None = None) -> list[dict]:
    limited = bars.tail(limit)
    frame_limited = frame.reindex(limited.index)
    rows: list[dict] = []
    for ts, row in limited.iterrows():
        ind = frame_limited.loc[ts] if ts in frame_limited.index else None
        d: dict = {
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
        if extra_cols and ind is not None:
            for col in extra_cols:
                val = ind.get(col)
                if val is None:
                    d[col] = None
                elif isinstance(val, (bool, np.bool_)):
                    d[col] = bool(val)
                elif isinstance(val, np.integer):
                    d[col] = int(val)
                elif isinstance(val, (float, np.floating)):
                    d[col] = None if not np.isfinite(float(val)) else float(val)
                else:
                    d[col] = val
        rows.append(d)
    return rows
