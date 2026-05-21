from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from typing import Any, Callable

import pandas as pd
from sqlalchemy import select

from backend.bot_loader import bot
from backend.db.database import AsyncSessionLocal
from backend.db.models import BacktestEquityPoint, BacktestRun, BacktestTrade, CustomStrategy
from backend.utils import bars_payload, clean, make_cfg
from strategies.base import StrategyContext

from .ai_verifier import make_openai_trade_verifier
from .config import BacktestConfig
from .engine import naive_equity_ts, naive_trade_row, run_backtest
from .strategy_loader import LoadedStrategy, compile_custom_strategy, load_builtin_strategy, strategy_code_warnings


ProgressCallback = Callable[[dict[str, Any]], None]


def _naive_ts(ts: pd.Timestamp | None) -> datetime | None:
    if ts is None or pd.isna(ts):
        return None
    if ts.tzinfo is not None:
        ts = ts.tz_convert(None)
    return ts.to_pydatetime()


def _sync_fetch_bars(cfg: BacktestConfig) -> tuple[pd.DataFrame, pd.Timestamp, str | None, str | None]:
    tf, _, bar_minutes = bot._normalize_timeframe(cfg.timeframe)
    limit = cfg.limit
    if limit is None:
        limit = max(60, min((cfg.lookback_days * 24 * 60) // max(1, bar_minutes) + 2, 100_000))
    return bot.fetch_closed_bars(
        cfg.pair,
        cfg.market,
        lookback_days=cfg.lookback_days,
        limit=limit,
        execution_mode=cfg.mode,
        timeframe=tf,
    )


async def load_strategy(cfg: BacktestConfig) -> LoadedStrategy:
    if cfg.custom_strategy_id:
        async with AsyncSessionLocal() as db:
            row = await db.get(CustomStrategy, cfg.custom_strategy_id)
        if row is None:
            raise ValueError("Custom strategy not found.")
        if not row.enabled:
            raise ValueError("Custom strategy is disabled.")
        return compile_custom_strategy(row)
    return load_builtin_strategy(cfg.strategy)


async def validate_custom_strategy(row: CustomStrategy) -> tuple[bool, str]:
    try:
        loaded = compile_custom_strategy(row)
        idx = pd.date_range("2026-01-01", periods=80, freq="15min", tz=bot.IST)
        close = pd.Series([100 + i * 0.05 for i in range(len(idx))], index=idx)
        sample = pd.DataFrame(
            {
                "Open": close.shift(1).fillna(close.iloc[0]),
                "High": close + 0.5,
                "Low": close - 0.5,
                "Close": close,
                "Volume": 1.0,
            },
            index=idx,
        )
        ctx = StrategyContext(
            pair="B-TEST_USDT",
            market="TESTUSDT",
            mode="futures",
            risk=10.0,
            allow_shorts=True,
            extras={},
        )
        result = loaded.analyze(sample, ctx)
        frame = result.get("frame") if isinstance(result, dict) else None
        if not isinstance(frame, pd.DataFrame):
            raise ValueError("Strategy did not return a frame DataFrame.")
        missing = [c for c in ("entry_side", "exit_long", "exit_short") if c not in frame.columns]
        if missing:
            raise ValueError(f"Strategy frame missing required columns: {', '.join(missing)}")
        warnings = strategy_code_warnings(row.code or "")
        if warnings:
            return True, "Strategy code is valid. Warnings: " + "; ".join(warnings)
        return True, "Strategy code is valid."
    except Exception as exc:
        return False, str(exc)


def _emit_progress(
    progress: ProgressCallback | None,
    *,
    stage: str,
    progress_value: float,
    message: str,
    event: str | None = None,
    stats: dict[str, Any] | None = None,
) -> None:
    if progress is None:
        return
    progress({
        "stage": stage,
        "progress": max(0.0, min(progress_value, 1.0)),
        "message": message,
        "event": event,
        "stats": stats or {},
    })


async def run_and_store_backtest(cfg: BacktestConfig, progress: ProgressCallback | None = None) -> dict:
    cfg = cfg.normalized()
    _emit_progress(progress, stage="strategy", progress_value=0.03, message="Loading strategy.")
    loaded = await load_strategy(cfg)
    _emit_progress(progress, stage="data", progress_value=0.08, message="Fetching historical candles.")
    bars, latest_closed, used_pair, used_source = await asyncio.to_thread(_sync_fetch_bars, cfg)
    if bars.empty:
        return {"ok": False, "message": "No candles returned for this pair/market."}
    _emit_progress(
        progress,
        stage="data",
        progress_value=0.16,
        message=f"Loaded {len(bars)} candles.",
        stats={"bars_total": len(bars)},
    )

    runtime_cfg = make_cfg(cfg.pair, cfg.market, cfg.mode, cfg.risk, cfg.lookback_days, timeframe=cfg.timeframe, exec_mode="paper")
    ctx = StrategyContext(
        pair=cfg.pair,
        market=cfg.market,
        mode=cfg.mode,
        risk=cfg.risk,
        allow_shorts=cfg.allow_shorts,
        extras={
            "latest_closed": latest_closed,
            "strategy_id": loaded.id,
            "strategy_title": loaded.title,
            "strategy_description": loaded.description,
        },
    )
    _emit_progress(progress, stage="analysis", progress_value=0.20, message="Running strategy script.")
    analysis = loaded.analyze(bars, ctx)
    frame = analysis["frame"]
    verifier = make_openai_trade_verifier(cfg) if cfg.ai_verification_enabled else None

    def engine_progress(update: dict[str, Any]) -> None:
        engine_pct = float(update.get("progress") or 0.0)
        _emit_progress(
            progress,
            stage=str(update.get("stage") or "simulating"),
            progress_value=0.24 + engine_pct * 0.62,
            message=str(update.get("message") or "Simulating trades."),
            event=update.get("event"),
            stats=update.get("stats") if isinstance(update.get("stats"), dict) else None,
        )

    _emit_progress(progress, stage="simulating", progress_value=0.24, message="Simulating broker fills.")
    result = await asyncio.to_thread(run_backtest, bars, frame, ctx, cfg, verifier, engine_progress)
    if not result.get("ok"):
        return result

    _emit_progress(progress, stage="storing", progress_value=0.90, message="Saving backtest result.")
    run_id = await store_backtest_result(
        cfg=cfg,
        loaded=loaded,
        bars=bars,
        data_source=used_source or used_pair or "coindcx",
        result=result,
    )
    _emit_progress(progress, stage="rendering", progress_value=0.95, message=f"Preparing Run #{run_id}.")
    meta = analysis.get("meta")
    chart_bars = bars_payload(
        bars,
        frame,
        min(len(bars), 1000),
        extra_cols=["entry_side", "exit_long", "exit_short", "reason"],
    )
    payload = clean({
        "ok": True,
        "run_id": run_id,
        "strategy": {
            "id": loaded.id,
            "name": loaded.title,
            "description": loaded.description,
            "version": loaded.version,
            "custom_strategy_id": loaded.custom_strategy_id,
            "chart_label": getattr(meta, "chart_label", "Strategy"),
            "score_label": getattr(meta, "score_label", "Score"),
        },
        "data": {
            "pair": cfg.pair,
            "market": cfg.market,
            "mode": cfg.mode,
            "timeframe": runtime_cfg.timeframe,
            "bars": len(bars),
            "used_pair": used_pair,
            "used_source": used_source,
            "start": bars.index.min(),
            "end": bars.index.max(),
        },
        "summary": result["summary"],
        "equity": result["equity"][-1500:],
        "trades": result["trades"][-500:],
        "events": result["events"][-100:],
        "bars": chart_bars,
        "config": cfg.to_dict(),
    })
    _emit_progress(progress, stage="completed", progress_value=1.0, message=f"Run #{run_id} completed.")
    return payload


async def run_walk_forward_validation(cfg: BacktestConfig, folds: int = 4) -> dict:
    cfg = cfg.normalized()
    folds = max(2, min(int(folds or 4), 10))
    loaded = await load_strategy(cfg)
    bars, latest_closed, used_pair, used_source = await asyncio.to_thread(_sync_fetch_bars, cfg)
    if bars.empty:
        return {"ok": False, "message": "No candles returned for this pair/market."}

    runtime_cfg = make_cfg(cfg.pair, cfg.market, cfg.mode, cfg.risk, cfg.lookback_days, timeframe=cfg.timeframe, exec_mode="paper")
    ctx = StrategyContext(
        pair=cfg.pair,
        market=cfg.market,
        mode=cfg.mode,
        risk=cfg.risk,
        allow_shorts=cfg.allow_shorts,
        extras={
            "latest_closed": latest_closed,
            "strategy_id": loaded.id,
            "strategy_title": loaded.title,
            "strategy_description": loaded.description,
        },
    )
    analysis = loaded.analyze(bars, ctx)
    frame = analysis["frame"]
    n = len(bars)
    min_test = max(30, cfg.warmup_bars + 10)
    if n < min_test * folds:
        return {
            "ok": False,
            "message": f"Need at least {min_test * folds} bars for {folds} walk-forward folds; got {n}.",
        }

    fold_size = n // (folds + 1)
    fold_results: list[dict[str, Any]] = []
    for fold in range(folds):
        train_end = fold_size * (fold + 1)
        test_start = train_end
        test_end = fold_size * (fold + 2) if fold < folds - 1 else n
        if test_end - test_start < min_test:
            continue
        test_bars = bars.iloc[test_start:test_end]
        test_frame = frame.reindex(test_bars.index)
        fold_cfg = replace(cfg, warmup_bars=0, finalize_open_trade=True)
        result = await asyncio.to_thread(run_backtest, test_bars, test_frame, ctx, fold_cfg, None, None)
        summary = result.get("summary", {}) if result.get("ok") else {}
        fold_results.append({
            "fold": fold + 1,
            "train_start": bars.index[0],
            "train_end": bars.index[train_end - 1],
            "test_start": test_bars.index[0],
            "test_end": test_bars.index[-1],
            "test_bars": len(test_bars),
            "summary": summary,
            "ok": bool(result.get("ok")),
        })

    if not fold_results:
        return {"ok": False, "message": "No valid walk-forward folds could be built."}

    sharpes = [
        float(f["summary"].get("portfolio_sharpe") if f["summary"].get("portfolio_sharpe") is not None else f["summary"].get("sharpe"))
        for f in fold_results
        if f["summary"].get("portfolio_sharpe") is not None or f["summary"].get("sharpe") is not None
    ]
    returns = [
        float(f["summary"].get("total_return_pct"))
        for f in fold_results
        if f["summary"].get("total_return_pct") is not None
    ]
    drawdowns = [
        float(f["summary"].get("max_drawdown_pct"))
        for f in fold_results
        if f["summary"].get("max_drawdown_pct") is not None
    ]
    trades = [int(f["summary"].get("trades") or 0) for f in fold_results]
    positive_return_folds = sum(1 for r in returns if r > 0)
    warnings: list[str] = []
    if sharpes and min(sharpes) < 0:
        warnings.append("At least one fold has negative portfolio Sharpe.")
    if returns and positive_return_folds < max(1, len(returns) // 2):
        warnings.append("Fewer than half the folds are profitable.")
    if trades and min(trades) < 10:
        warnings.append("At least one fold has fewer than 10 trades; fold-level statistics are weak.")

    aggregate = {
        "folds": len(fold_results),
        "positive_return_folds": positive_return_folds,
        "avg_return_pct": round(sum(returns) / len(returns), 2) if returns else None,
        "median_return_pct": round(float(pd.Series(returns).median()), 2) if returns else None,
        "avg_portfolio_sharpe": round(sum(sharpes) / len(sharpes), 3) if sharpes else None,
        "median_portfolio_sharpe": round(float(pd.Series(sharpes).median()), 3) if sharpes else None,
        "worst_drawdown_pct": round(min(drawdowns), 2) if drawdowns else None,
        "total_trades": sum(trades),
        "assessment": "robust" if not warnings and positive_return_folds == len(fold_results) else "fragile",
        "warnings": warnings,
    }
    return clean({
        "ok": True,
        "strategy": {
            "id": loaded.id,
            "name": loaded.title,
            "version": loaded.version,
            "custom_strategy_id": loaded.custom_strategy_id,
        },
        "data": {
            "pair": cfg.pair,
            "market": cfg.market,
            "mode": cfg.mode,
            "timeframe": runtime_cfg.timeframe,
            "bars": len(bars),
            "used_pair": used_pair,
            "used_source": used_source,
            "start": bars.index.min(),
            "end": bars.index.max(),
        },
        "config": cfg.to_dict(),
        "aggregate": aggregate,
        "folds": fold_results,
    })


async def store_backtest_result(
    cfg: BacktestConfig,
    loaded: LoadedStrategy,
    bars: pd.DataFrame,
    data_source: str,
    result: dict,
) -> int:
    async with AsyncSessionLocal() as db:
        run = BacktestRun(
            pair=cfg.pair,
            market=cfg.market,
            mode=cfg.mode,
            timeframe=cfg.timeframe,
            strategy=loaded.id,
            custom_strategy_id=loaded.custom_strategy_id,
            custom_strategy_title=loaded.title if loaded.custom_strategy_id else None,
            custom_strategy_version=loaded.version,
            custom_strategy_code_snapshot=loaded.code_snapshot,
            data_source=data_source,
            start_ts=_naive_ts(bars.index.min()),
            end_ts=_naive_ts(bars.index.max()),
            bars_count=len(bars),
            config=cfg.to_dict(),
            summary=result["summary"],
        )
        db.add(run)
        await db.flush()
        for item in result["trades"]:
            t = naive_trade_row(item)
            db.add(BacktestTrade(
                run_id=run.id,
                side=str(t.get("side") or ""),
                entry_ts=t.get("entry_ts"),
                exit_ts=t.get("exit_ts"),
                entry_px=float(t.get("entry_px") or 0.0),
                exit_px=float(t["exit_px"]) if t.get("exit_px") is not None else None,
                qty=float(t.get("qty") or 0.0),
                gross_pnl=float(t.get("gross_pnl") or 0.0),
                fees=float(t.get("fees") or 0.0),
                net_pnl=float(t.get("net_pnl") or 0.0),
                return_pct=float(t["return_pct"]) if t.get("return_pct") is not None else None,
                r_multiple=float(t["r_multiple"]) if t.get("r_multiple") is not None else None,
                exit_reason=t.get("exit_reason"),
                payload=clean(item),
            ))
        for item in result["equity"]:
            db.add(BacktestEquityPoint(
                run_id=run.id,
                ts=naive_equity_ts(item["time"]).to_pydatetime(),
                equity=float(item.get("equity") or 0.0),
                cash=float(item.get("cash") or 0.0),
                position_value=float(item.get("position_value") or 0.0),
                drawdown_pct=float(item.get("drawdown_pct") or 0.0),
            ))
        await db.commit()
        return int(run.id)


async def get_backtest_run(run_id: int) -> dict | None:
    async with AsyncSessionLocal() as db:
        run = await db.get(BacktestRun, run_id)
        if run is None:
            return None
        trades_result = await db.execute(select(BacktestTrade).where(BacktestTrade.run_id == run_id).order_by(BacktestTrade.entry_ts.asc()))
        equity_result = await db.execute(select(BacktestEquityPoint).where(BacktestEquityPoint.run_id == run_id).order_by(BacktestEquityPoint.ts.asc()))
        trades = trades_result.scalars().all()
        equity = equity_result.scalars().all()
    return clean({
        "ok": True,
        "run_id": run.id,
        "strategy": {
            "id": run.strategy,
            "name": run.custom_strategy_title or run.strategy,
            "custom_strategy_id": run.custom_strategy_id,
            "version": run.custom_strategy_version,
        },
        "data": {
            "pair": run.pair,
            "market": run.market,
            "mode": run.mode,
            "timeframe": run.timeframe,
            "bars": run.bars_count,
            "start": run.start_ts,
            "end": run.end_ts,
            "source": run.data_source,
        },
        "summary": run.summary,
        "config": run.config,
        "trades": [t.payload for t in trades],
        "equity": [
            {
                "time": e.ts,
                "equity": e.equity,
                "cash": e.cash,
                "position_value": e.position_value,
                "drawdown_pct": e.drawdown_pct,
            }
            for e in equity
        ],
    })
