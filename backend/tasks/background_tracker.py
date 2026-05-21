"""APScheduler job: keep saved futures tracker coins scanned without a browser."""
from __future__ import annotations

import asyncio
import logging
import os

import pandas as pd
from sqlalchemy import select

from ..bot_loader import bot
from ..db.database import AsyncSessionLocal
from ..db.models import CustomStrategy, UserSetting
from ..db.persistence import store_tracker_signal_events, store_trade
from ..utils import clean, coin_from_pair, futures_market_for_coin, futures_pair_for_coin, make_cfg
from strategies.base import StrategyContext
from strategies.registry import get_strategy, normalize_strategy_id

logger = logging.getLogger(__name__)

_EXECUTION_STATE: dict[str, dict[str, object]] = {}
EXECUTABLE_STRATEGIES = {
    "confluence",
    "daily_sweep",
    "trend_following",
    "mean_reversion",
    "mixed_consensus",
    "volatility_squeeze",
    "precision_momentum",
    "volume_profile",
}
SCANNER_ONLY_STRATEGIES = {"arbitrage", "funding_basis", "pairs_stat_arb"}


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


async def _load_dashboard_settings() -> dict:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(UserSetting).where(UserSetting.key == "dashboard"))
        row = result.scalar_one_or_none()
    return row.payload if row and isinstance(row.payload, dict) else {}


async def _load_custom_analyzer(custom_strategy_id: int | None):
    """Return compiled custom strategy analyze function or None if not found."""
    if not custom_strategy_id:
        return None
    from ..backtesting.strategy_loader import compile_custom_strategy
    from sqlalchemy import select as sa_select
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                sa_select(CustomStrategy).where(
                    CustomStrategy.id == custom_strategy_id,
                    CustomStrategy.enabled.is_(True),
                )
            )
            row = result.scalar_one_or_none()
        if row is None:
            logger.warning("Custom strategy id=%s not found or disabled.", custom_strategy_id)
            return None
        loaded = compile_custom_strategy(row)
        return loaded
    except Exception as exc:
        logger.warning("Failed to compile custom strategy id=%s: %s", custom_strategy_id, exc)
        return None


def _normalise_coins(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    coins: list[str] = []
    for item in raw:
        text = str(item).strip().upper()
        if not text:
            continue
        coin = coin_from_pair(text) if ("_" in text or "-" in text) else "".join(ch for ch in text if ch.isalnum())
        if coin and coin not in coins:
            coins.append(coin)
        if len(coins) >= 12:
            break
    return coins


def _snapshot_one(coin: str, strategy_id: str, risk: float, lookback_days: int, timeframe: str | None = None) -> dict:
    # Import here to avoid making router import order part of application startup.
    from ..routers.snapshot import _sync_build_snapshot

    pair = futures_pair_for_coin(coin)
    market = futures_market_for_coin(coin)
    snap = _sync_build_snapshot(pair, market, strategy_id, "futures", lookback_days, 80, risk, timeframe=timeframe)
    strategy_info = snap.get("strategy") or {}
    stats = snap.get("stats") or {}
    action = snap.get("action") or {}
    state = snap.get("state") or {}
    ticker = snap.get("ticker") or {}
    return clean({
        "ok": bool(snap.get("ok")),
        "coin": snap.get("coin") or coin,
        "pair": snap.get("pair") or pair,
        "market": snap.get("market") or market,
        "strategy": strategy_info.get("id"),
        "strategy_name": strategy_info.get("name"),
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
        "freshness_minutes": snap.get("freshness_minutes"),
        "latest_time": stats.get("latest_time"),
        "server_time": pd.Timestamp.now(tz=bot.IST),
        "source": "background_tracker",
    })


def _build_strategy_frame(strategy_id: str, bars: pd.DataFrame, cfg: object, custom_analyzer=None) -> pd.DataFrame:
    ctx = StrategyContext(
        pair=cfg.pair,
        market=cfg.market,
        mode=cfg.execution_mode,
        risk=cfg.risk_dollars,
        allow_shorts=cfg.allow_shorts,
        extras={},
    )
    analyzer = custom_analyzer.analyze if custom_analyzer is not None else get_strategy(strategy_id)
    analysis = analyzer(bars, ctx)
    frame = analysis.get("frame")
    if not isinstance(frame, pd.DataFrame):
        return pd.DataFrame()
    return frame.dropna(subset=["Close"]).copy()


def _trade_from_open_state(pair: str, state: object, cfg: object, strategy_id: str) -> dict | None:
    if not getattr(state, "entry_ts", None) or not getattr(state, "side", 0):
        return None
    return {
        "pair": pair,
        "side": int(state.side),
        "entry_ts": state.entry_ts,
        "exit_ts": None,
        "entry_px": float(state.entry_px or 0.0),
        "exit_px": None,
        "stop_px": float(state.stop_px or 0.0),
        "target_px": float(state.target_px or 0.0),
        "qty": float(state.qty or 0.0),
        "risk_usd": float(getattr(cfg, "risk_dollars", 0.0) or 0.0),
        "pnl": None,
        "exit_reason": None,
        "mode": "futures",
        "strategy": strategy_id,
        "execution_mode": "paper",
    }


def _process_bar_and_capture_trade(
    ts: pd.Timestamp,
    row: pd.Series,
    state: object,
    cfg: object,
    frame: pd.DataFrame,
    pair: str,
    strategy_id: str,
) -> tuple[list[str], list[dict]]:
    before_side = int(getattr(state, "side", 0) or 0)
    before_entry_ts = getattr(state, "entry_ts", None)
    events = bot.process_closed_bar(ts, row, state, cfg, frame.loc[:ts])
    after_side = int(getattr(state, "side", 0) or 0)
    after_entry_ts = getattr(state, "entry_ts", None)
    trades: list[dict] = []
    if before_side == 0 and after_side != 0 and after_entry_ts is not None and after_entry_ts != before_entry_ts:
        trade = _trade_from_open_state(pair, state, cfg, strategy_id)
        if trade:
            trades.append(trade)
    return events, trades


def _execute_one(
    coin: str,
    strategy_id: str,
    risk: float,
    lookback_days: int,
    timeframe: str | None = None,
    exec_mode: str = "paper",
    custom_analyzer=None,
) -> tuple[list[str], list[dict]]:
    pair = futures_pair_for_coin(coin)
    market = futures_market_for_coin(coin)
    if custom_analyzer is None:
        strategy_id = normalize_strategy_id(strategy_id)
        if strategy_id not in EXECUTABLE_STRATEGIES:
            return [f"{pair}: background executor skipped scanner-only strategy {strategy_id}."], []

    cfg = make_cfg(pair, market, "futures", risk, lookback_days, timeframe=timeframe, exec_mode="paper", strategy=strategy_id)
    cfg.allow_shorts = _env_bool("BACKGROUND_ALLOW_SHORTS", False)

    state_key = f"{pair}|{strategy_id}|{getattr(custom_analyzer, 'custom_strategy_id', '') or 'builtin'}"
    slot = _EXECUTION_STATE.setdefault(state_key, {"state": bot.TradeState(), "last_ts": None})
    state = slot["state"]
    last_ts = slot["last_ts"]
    assert isinstance(state, bot.TradeState)

    bars, latest_closed, used_pair, used_source = bot.fetch_closed_bars(
        cfg.pair,
        cfg.market,
        lookback_days=cfg.lookback_days,
        execution_mode=cfg.execution_mode,
        timeframe=cfg.timeframe,
    )
    if bars.empty:
        return [f"{pair}: no bars fetched for background execution."], []
    bars = bars[bars.index <= latest_closed]
    frame = _build_strategy_frame(strategy_id, bars, cfg, custom_analyzer=custom_analyzer)
    if frame.empty:
        return [f"{pair}: {strategy_id} warmup in progress."], []

    events: list[str] = []
    opened_trades: list[dict] = []
    if last_ts is None:
        boot_cfg = bot.RuntimeConfig(**{**cfg.__dict__, "place_orders": False})
        for ts, row in frame.iterrows():
            bot.process_closed_bar(ts, row, state, boot_cfg, frame.loc[:ts])
            slot["last_ts"] = ts
        bot.drain_completed_trades()  # discard historical bootstrap trades
        last_ts = slot["last_ts"]
        if isinstance(last_ts, pd.Timestamp):
            events.append(f"{pair}: {strategy_id} background executor bootstrapped to {bot._fmt_ts(last_ts)}.")
            state = bot.TradeState(realized_pnl=state.realized_pnl)
            slot["state"] = state
            for ev in bot.sync_futures_position_state(last_ts, state, cfg):
                events.append(ev)
            last_row = frame.loc[last_ts]
            bar_events, trades = _process_bar_and_capture_trade(
                last_ts, last_row, state, cfg, frame, pair, strategy_id
            )
            events.extend(bar_events)
            opened_trades.extend(trades)
        return events, opened_trades

    assert isinstance(last_ts, pd.Timestamp)
    new_rows = frame[frame.index > last_ts]
    for ts, row in new_rows.iterrows():
        for ev in bot.sync_futures_position_state(ts, state, cfg):
            events.append(ev)
        bar_events, trades = _process_bar_and_capture_trade(ts, row, state, cfg, frame, pair, strategy_id)
        events.extend(bar_events)
        opened_trades.extend(trades)
        slot["last_ts"] = ts
    if used_pair or used_source:
        cfg.candle_pair = used_pair
        cfg.data_source = used_source
    return events, opened_trades


def _paper_strategy_selections(settings: dict, fallback_strategy_id: str) -> list[str]:
    raw = settings.get("paperStrategies")
    if isinstance(raw, list):
        selections = [str(item).strip() for item in raw if str(item).strip()]
    else:
        selections = []
    if not selections:
        custom_id = settings.get("customStrategyId")
        selections = [f"custom:{custom_id}" if custom_id else f"builtin:{fallback_strategy_id}"]
    return selections[:20]


async def _resolve_strategy_selection(selection: str, fallback_strategy_id: str):
    kind, _, raw_id = selection.partition(":")
    if not raw_id:
        kind, raw_id = "builtin", kind
    if kind == "custom":
        try:
            custom_analyzer = await _load_custom_analyzer(int(raw_id))
        except (TypeError, ValueError):
            custom_analyzer = None
        if custom_analyzer is None:
            logger.warning("Custom paper strategy %s unavailable; skipping.", raw_id)
            return None, None, None
        return custom_analyzer.id, custom_analyzer, selection
    strategy_id = normalize_strategy_id(raw_id or fallback_strategy_id)
    if strategy_id in SCANNER_ONLY_STRATEGIES or strategy_id not in EXECUTABLE_STRATEGIES:
        logger.info("Paper strategy skipped: %s is scanner-only or not executable.", strategy_id)
        return None, None, None
    return strategy_id, None, f"builtin:{strategy_id}"


async def _execute_saved_tracker_orders(settings: dict, coins: list[str], strategy_id: str, risk: float, lookback_days: int, timeframe: str | None = None) -> None:
    # This path is intentionally paper-only: it watches live candles and stores
    # simulated orders per selected strategy without touching the exchange.
    exec_mode = "paper"

    custom_analyzer = None
    resolved = []
    for selection in _paper_strategy_selections(settings, strategy_id):
        item = await _resolve_strategy_selection(selection, strategy_id)
        if item[0]:
            resolved.append(item)
    if not resolved:
        return

    max_symbols = max(1, min(int(float(os.getenv("BACKGROUND_EXECUTOR_MAX_SYMBOLS", "3"))), 12))
    for effective_strategy_id, custom_analyzer, selection in resolved:
        logger.info("Paper executor watching %s for strategy %s.", ",".join(coins[:max_symbols]), selection)
        for coin in coins[:max_symbols]:
            try:
                events, opened_trades = await asyncio.to_thread(
                    _execute_one, coin, effective_strategy_id, risk, lookback_days, timeframe, exec_mode, custom_analyzer
                )
                for event in events:
                    logger.info("Paper executor [%s]: %s", effective_strategy_id, event)

                for trade in opened_trades:
                    try:
                        await store_trade(trade)
                    except Exception as exc:
                        logger.warning("Failed to store open paper trade for %s/%s: %s", coin, effective_strategy_id, exc)

                pending = bot.drain_completed_trades()
                for trade in pending:
                    trade.setdefault("strategy", effective_strategy_id)
                    trade["execution_mode"] = "paper"
                    if trade.get("mode") == "spot":
                        trade["mode"] = "futures"
                    try:
                        await store_trade(trade)
                    except Exception as exc:
                        logger.warning("Failed to store closed paper trade for %s/%s: %s", coin, effective_strategy_id, exc)
                total = len(opened_trades) + len(pending)
                if total:
                    logger.info("Paper executor stored %d order update(s) for %s [%s].", total, coin, effective_strategy_id)
            except Exception as exc:
                logger.warning("Paper executor failed for %s/%s: %s", coin, effective_strategy_id, exc)


async def scan_saved_tracker_coins() -> None:
    settings = await _load_dashboard_settings()
    if not settings.get("trackingActive"):
        return

    coins = _normalise_coins(settings.get("selectedCoins"))
    if not coins:
        return

    strategy_id = str(settings.get("strategy") or "confluence")
    try:
        risk = max(0.01, min(float(settings.get("risk") or 10.0), 1_000_000.0))
    except (TypeError, ValueError):
        risk = 10.0
    try:
        lookback_days = max(1, min(int(float(settings.get("lookback") or 2)), 14))
    except (TypeError, ValueError):
        lookback_days = 2
    try:
        timeframe, _, _ = bot._normalize_timeframe(settings.get("timeframe") or None)
    except Exception:
        timeframe, _, _ = bot._normalize_timeframe(None)

    rows: list[dict] = []
    for coin in coins:
        try:
            rows.append(await asyncio.to_thread(_snapshot_one, coin, strategy_id, risk, lookback_days, timeframe))
        except Exception as exc:
            logger.warning("Background tracker failed for %s: %s", coin, exc)

    if rows:
        await store_tracker_signal_events(rows)
        logger.info("Background tracker stored %d signal rows for %s", len(rows), ",".join(coins))

    await _execute_saved_tracker_orders(settings, coins, strategy_id, risk, lookback_days, timeframe)
