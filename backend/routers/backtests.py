from __future__ import annotations

import asyncio
import csv
import io
from collections import defaultdict
from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.backtesting.config import BacktestConfig
from backend.backtesting.jobs import complete_job, create_job, fail_job, job_payload, update_job
from backend.backtesting.service import (
    get_backtest_run,
    run_and_store_backtest,
    run_walk_forward_validation,
    validate_custom_strategy,
)
from backend.backtesting.strategy_loader import validate_slug
from backend.db.database import AsyncSessionLocal
from backend.db.models import BacktestEquityPoint, BacktestRun, BacktestTrade, CustomStrategy
from backend.utils import clean
from strategies.registry import list_strategies

router = APIRouter(prefix="/api/backtests", tags=["backtests"])


class CustomStrategyPayload(BaseModel):
    title: str
    slug: str
    description: str = ""
    code: str
    enabled: bool = True


class CustomStrategyPatch(BaseModel):
    title: str | None = None
    slug: str | None = None
    description: str | None = None
    code: str | None = None
    enabled: bool | None = None


class BacktestRequest(BaseModel):
    pair: str = "B-ETH_USDT"
    market: str = "ETHUSDT"
    mode: str = "futures"
    strategy: str = "confluence"
    custom_strategy_id: int | None = None
    timeframe: str = "15m"
    lookback_days: int = 30
    initial_capital: float = 10_000
    risk: float = 10
    commission_bps: float = 5
    spread_bps: float = 0
    slippage_bps: float = 0
    leverage: float = 1
    risk_reward_ratio: float = 2
    target_mode: str = "strategy_or_rr"
    allow_shorts: bool = True
    fill_model: str = "next_open"
    position_sizing: str = "risk_fixed"
    risk_mode: str = "fixed_amount"
    opposite_signal_mode: str = "ignore"
    same_bar_priority: str = "stop_first"
    finalize_open_trade: bool = True
    warmup_bars: int = 50
    min_signal_score: float = 0.0
    ai_verification_enabled: bool = False
    ai_min_confidence: float = 70
    ai_candles: int = 80
    ai_model: str = "gpt-5.4-mini"
    limit: int | None = None


class WalkForwardRequest(BacktestRequest):
    folds: int = 4


class PaperEvaluationRequest(BacktestRequest):
    strategies: list[str] = []


def _custom_strategy_payload(row: CustomStrategy, include_code: bool = False) -> dict:
    payload = {
        "id": row.id,
        "title": row.title,
        "slug": row.slug,
        "description": row.description,
        "version": row.version,
        "enabled": row.enabled,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "last_validated_at": row.last_validated_at,
        "validation_status": row.validation_status,
        "validation_message": row.validation_message,
    }
    if include_code:
        payload["code"] = row.code
    return clean(payload)


@router.get("/strategies")
async def strategies() -> JSONResponse:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(CustomStrategy).order_by(CustomStrategy.updated_at.desc()))
        custom = result.scalars().all()
    builtins = list_strategies()
    return JSONResponse(clean({
        "ok": True,
        "builtins": builtins,
        "custom": [_custom_strategy_payload(row) for row in custom],
        "strategies": [
            *[{**s, "kind": "builtin", "title": s.get("name")} for s in builtins],
            *[{**_custom_strategy_payload(row), "kind": "custom"} for row in custom],
        ],
    }))


@router.post("/custom-strategies")
async def create_custom_strategy(body: CustomStrategyPayload) -> JSONResponse:
    title = body.title.strip()
    if not title:
        return JSONResponse({"ok": False, "message": "Title is required."}, status_code=400)
    try:
        slug = validate_slug(body.slug)
    except ValueError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    row = CustomStrategy(
        title=title,
        slug=slug,
        description=body.description.strip(),
        code=body.code,
        enabled=body.enabled,
        version=1,
    )
    ok, message = await validate_custom_strategy(row)
    row.validation_status = "valid" if ok else "invalid"
    row.validation_message = message
    row.last_validated_at = datetime.utcnow()
    async with AsyncSessionLocal() as db:
        db.add(row)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            return JSONResponse({"ok": False, "message": "Slug already exists."}, status_code=409)
        await db.refresh(row)
    return JSONResponse(clean({"ok": ok, "message": message, "strategy": _custom_strategy_payload(row, include_code=True)}))


@router.get("/custom-strategies/{strategy_id}")
async def get_custom_strategy(strategy_id: int) -> JSONResponse:
    async with AsyncSessionLocal() as db:
        row = await db.get(CustomStrategy, strategy_id)
    if row is None:
        return JSONResponse({"ok": False, "message": "Custom strategy not found."}, status_code=404)
    return JSONResponse(clean({"ok": True, "strategy": _custom_strategy_payload(row, include_code=True)}))


@router.put("/custom-strategies/{strategy_id}")
async def update_custom_strategy(strategy_id: int, body: CustomStrategyPatch) -> JSONResponse:
    async with AsyncSessionLocal() as db:
        row = await db.get(CustomStrategy, strategy_id)
        if row is None:
            return JSONResponse({"ok": False, "message": "Custom strategy not found."}, status_code=404)
        if body.title is not None:
            title = body.title.strip()
            if not title:
                return JSONResponse({"ok": False, "message": "Title is required."}, status_code=400)
            row.title = title
        if body.slug is not None:
            try:
                row.slug = validate_slug(body.slug)
            except ValueError as exc:
                return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        code_changed = body.code is not None and body.code != row.code
        if body.description is not None:
            row.description = body.description.strip()
        if body.code is not None:
            row.code = body.code
        if body.enabled is not None:
            row.enabled = body.enabled
        if code_changed:
            row.version = int(row.version or 1) + 1
        ok, message = await validate_custom_strategy(row)
        row.validation_status = "valid" if ok else "invalid"
        row.validation_message = message
        row.last_validated_at = datetime.utcnow()
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            return JSONResponse({"ok": False, "message": "Slug already exists."}, status_code=409)
        await db.refresh(row)
    return JSONResponse(clean({"ok": ok, "message": message, "strategy": _custom_strategy_payload(row, include_code=True)}))


@router.post("/custom-strategies/{strategy_id}/validate")
async def validate_strategy(strategy_id: int) -> JSONResponse:
    async with AsyncSessionLocal() as db:
        row = await db.get(CustomStrategy, strategy_id)
        if row is None:
            return JSONResponse({"ok": False, "message": "Custom strategy not found."}, status_code=404)
        ok, message = await validate_custom_strategy(row)
        row.validation_status = "valid" if ok else "invalid"
        row.validation_message = message
        row.last_validated_at = datetime.utcnow()
        await db.commit()
        await db.refresh(row)
    return JSONResponse(clean({"ok": ok, "message": message, "strategy": _custom_strategy_payload(row)}))


@router.post("/run")
async def run_backtest(body: BacktestRequest) -> JSONResponse:
    payload = body.model_dump() if hasattr(body, "model_dump") else body.dict()
    cfg = BacktestConfig(**payload).normalized()
    try:
        result = await run_and_store_backtest(cfg)
    except Exception as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    return JSONResponse(clean(result), status_code=200 if result.get("ok") else 400)


@router.post("/walk-forward")
async def walk_forward(body: WalkForwardRequest) -> JSONResponse:
    payload = body.model_dump() if hasattr(body, "model_dump") else body.dict()
    folds = int(payload.pop("folds", 4) or 4)
    cfg = BacktestConfig(**payload).normalized()
    try:
        result = await run_walk_forward_validation(cfg, folds=folds)
    except Exception as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    return JSONResponse(clean(result), status_code=200 if result.get("ok") else 400)


@router.post("/paper-evaluate")
async def paper_evaluate(body: PaperEvaluationRequest) -> JSONResponse:
    payload = body.model_dump() if hasattr(body, "model_dump") else body.dict()
    selections = [str(item).strip() for item in payload.pop("strategies", []) if str(item).strip()]
    if not selections:
        selected = payload.get("strategy") or "confluence"
        selections = [f"builtin:{selected}"]

    base_payload = dict(payload)
    base_payload["ai_verification_enabled"] = bool(base_payload.get("ai_verification_enabled", False))
    results: list[dict] = []

    for selected in selections[:20]:
        kind, _, raw_id = selected.partition(":")
        if not raw_id:
            kind, raw_id = "builtin", kind
        run_payload = dict(base_payload)
        if kind == "custom":
            try:
                custom_id = int(raw_id)
            except (TypeError, ValueError):
                results.append({"ok": False, "selection": selected, "message": "Invalid custom strategy id."})
                continue
            run_payload["custom_strategy_id"] = custom_id
            run_payload["strategy"] = "custom"
        else:
            run_payload["custom_strategy_id"] = None
            run_payload["strategy"] = raw_id

        cfg = BacktestConfig(**run_payload).normalized()
        try:
            result = await run_and_store_backtest(cfg)
        except Exception as exc:
            results.append({"ok": False, "selection": selected, "message": str(exc)})
            continue
        if not result.get("ok"):
            results.append({"ok": False, "selection": selected, "message": result.get("message", "Paper evaluation failed.")})
            continue
        summary = result.get("summary") or {}
        trades = result.get("trades") or []
        latest_trade = trades[-1] if trades else None
        results.append({
            "ok": True,
            "selection": selected,
            "run_id": result.get("run_id"),
            "strategy": result.get("strategy"),
            "data": result.get("data"),
            "summary": summary,
            "latest_order": latest_trade,
            "trades": trades[-25:],
            "events": (result.get("events") or [])[-25:],
        })

    successful = [r for r in results if r.get("ok")]
    return JSONResponse(clean({
        "ok": bool(results),
        "mode": "paper",
        "count": len(results),
        "successful": len(successful),
        "failed": len(results) - len(successful),
        "results": results,
    }), status_code=200 if results else 400)


async def _run_backtest_job(job_id: str, cfg: BacktestConfig) -> None:
    update_job(job_id, status="running", stage="starting", progress=0.01, message="Starting backtest.")

    def progress_cb(update: dict) -> None:
        update_job(
            job_id,
            stage=str(update.get("stage") or "running"),
            progress=float(update.get("progress") or 0.0),
            message=str(update.get("message") or "Running backtest."),
            event=update.get("event"),
            stats=update.get("stats") if isinstance(update.get("stats"), dict) else None,
        )

    try:
        result = await run_and_store_backtest(cfg, progress_cb)
    except Exception as exc:
        fail_job(job_id, str(exc))
        return
    complete_job(job_id, result)


@router.post("/run/start")
async def start_backtest(body: BacktestRequest) -> JSONResponse:
    payload = body.model_dump() if hasattr(body, "model_dump") else body.dict()
    cfg = BacktestConfig(**payload).normalized()
    job = create_job(cfg.to_dict())
    asyncio.create_task(_run_backtest_job(str(job["job_id"]), cfg))
    return JSONResponse(job, status_code=202)


@router.get("/jobs/{job_id}")
async def get_backtest_job(job_id: str) -> JSONResponse:
    job = job_payload(job_id)
    if job is None:
        return JSONResponse({"ok": False, "message": "Backtest job not found."}, status_code=404)
    return JSONResponse(job)


@router.get("/runs")
async def list_runs(limit: int = 25) -> JSONResponse:
    limit = max(1, min(limit, 100))
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(BacktestRun).order_by(BacktestRun.created_at.desc()).limit(limit))
        rows = result.scalars().all()
    return JSONResponse(clean({
        "ok": True,
        "runs": [
            {
                "id": row.id,
                "created_at": row.created_at,
                "pair": row.pair,
                "mode": row.mode,
                "timeframe": row.timeframe,
                "strategy": row.custom_strategy_title or row.strategy,
                "bars": row.bars_count,
                "summary": row.summary,
            }
            for row in rows
        ],
    }))


@router.get("/compare")
async def compare_runs(run_ids: str = "", limit: int = 10) -> JSONResponse:
    ids: list[int] = []
    for raw in (run_ids or "").split(","):
        raw = raw.strip()
        if raw.isdigit():
            ids.append(int(raw))
    async with AsyncSessionLocal() as db:
        if ids:
            result = await db.execute(select(BacktestRun).where(BacktestRun.id.in_(ids)))
        else:
            limit = max(2, min(limit, 25))
            result = await db.execute(select(BacktestRun).order_by(BacktestRun.created_at.desc()).limit(limit))
        rows = result.scalars().all()
    if not rows:
        return JSONResponse({"ok": False, "message": "No backtest runs found."}, status_code=404)

    def metric(row: BacktestRun, key: str) -> float | None:
        value = (row.summary or {}).get(key)
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    comparisons = []
    for row in sorted(rows, key=lambda r: r.created_at or datetime.min):
        summary = row.summary or {}
        comparisons.append({
            "id": row.id,
            "created_at": row.created_at,
            "strategy": row.custom_strategy_title or row.strategy,
            "pair": row.pair,
            "market": row.market,
            "timeframe": row.timeframe,
            "bars": row.bars_count,
            "return_pct": summary.get("total_return_pct"),
            "portfolio_sharpe": summary.get("portfolio_sharpe", summary.get("sharpe")),
            "active_sharpe": summary.get("active_sharpe"),
            "max_drawdown_pct": summary.get("max_drawdown_pct"),
            "win_rate_pct": summary.get("win_rate_pct"),
            "profit_factor": summary.get("profit_factor"),
            "trades": summary.get("trades"),
            "exposure_pct": summary.get("exposure_pct"),
            "warnings": summary.get("assumption_warnings", []),
        })

    best_return = max(rows, key=lambda r: metric(r, "total_return_pct") if metric(r, "total_return_pct") is not None else float("-inf"))
    best_sharpe = max(rows, key=lambda r: metric(r, "portfolio_sharpe") if metric(r, "portfolio_sharpe") is not None else (metric(r, "sharpe") if metric(r, "sharpe") is not None else float("-inf")))
    best_drawdown = max(rows, key=lambda r: metric(r, "max_drawdown_pct") if metric(r, "max_drawdown_pct") is not None else float("-inf"))
    return JSONResponse(clean({
        "ok": True,
        "count": len(comparisons),
        "best": {
            "return_run_id": best_return.id,
            "sharpe_run_id": best_sharpe.id,
            "drawdown_run_id": best_drawdown.id,
        },
        "runs": comparisons,
    }))


@router.get("/{run_id}/diagnostics")
async def run_diagnostics(run_id: int) -> JSONResponse:
    async with AsyncSessionLocal() as db:
        run = await db.get(BacktestRun, run_id)
        if run is None:
            return JSONResponse({"ok": False, "message": "Backtest run not found."}, status_code=404)
        trades_result = await db.execute(
            select(BacktestTrade).where(BacktestTrade.run_id == run_id).order_by(BacktestTrade.entry_ts.asc())
        )
        equity_result = await db.execute(
            select(BacktestEquityPoint).where(BacktestEquityPoint.run_id == run_id).order_by(BacktestEquityPoint.ts.asc())
        )
        trades = trades_result.scalars().all()
        equity = equity_result.scalars().all()

    side_rows: dict[str, list[BacktestTrade]] = defaultdict(list)
    hour_rows: dict[int, list[BacktestTrade]] = defaultdict(list)
    reason_counts: dict[str, int] = defaultdict(int)
    for trade in trades:
        side_rows[trade.side].append(trade)
        if trade.entry_ts is not None:
            hour_rows[int(trade.entry_ts.hour)].append(trade)
        reason_counts[str(trade.exit_reason or "UNKNOWN")] += 1

    def trade_stats(rows: list[BacktestTrade]) -> dict:
        if not rows:
            return {"trades": 0, "win_rate_pct": None, "net_pnl": 0.0, "avg_pnl": None}
        wins = [t for t in rows if float(t.net_pnl or 0.0) > 0]
        net = sum(float(t.net_pnl or 0.0) for t in rows)
        return {
            "trades": len(rows),
            "win_rate_pct": round(len(wins) / len(rows) * 100, 2),
            "net_pnl": round(net, 2),
            "avg_pnl": round(net / len(rows), 2),
        }

    hourly = [
        {"hour": hour, **trade_stats(rows)}
        for hour, rows in sorted(hour_rows.items())
    ]
    best_hours = sorted(hourly, key=lambda r: r["avg_pnl"] if r["avg_pnl"] is not None else -10**12, reverse=True)[:3]
    worst_hours = sorted(hourly, key=lambda r: r["avg_pnl"] if r["avg_pnl"] is not None else 10**12)[:3]

    gross_profit = sum(float(t.gross_pnl or 0.0) for t in trades if float(t.gross_pnl or 0.0) > 0)
    total_fees = sum(float(t.fees or 0.0) for t in trades)
    fee_pct = (total_fees / gross_profit * 100) if gross_profit > 0 else None
    worst_equity = min(equity, key=lambda e: float(e.drawdown_pct or 0.0), default=None)
    summary = run.summary or {}
    recommendations: list[str] = []
    recommendations.extend(summary.get("assumption_warnings") or [])
    if float(summary.get("profit_factor") or 0.0) < 1.2:
        recommendations.append("Profit factor is weak; inspect exits, costs, and low-quality signal filters first.")
    if int(summary.get("trades") or 0) < 30:
        recommendations.append("Trade count is low; validate on a longer sample before tuning parameters.")
    if fee_pct is not None and fee_pct > 25:
        recommendations.append("Fees consume more than 25% of gross profit; test lower-frequency entries or wider targets.")
    if hourly and best_hours and worst_hours:
        recommendations.append("Use hourly diagnostics to test a time-of-day filter in a separate experiment.")

    return JSONResponse(clean({
        "ok": True,
        "run_id": run.id,
        "strategy": run.custom_strategy_title or run.strategy,
        "summary": summary,
        "side_stats": {side: trade_stats(rows) for side, rows in sorted(side_rows.items())},
        "hourly_stats": hourly,
        "best_hours": best_hours,
        "worst_hours": worst_hours,
        "exit_reasons": dict(sorted(reason_counts.items())),
        "fee_impact": {
            "gross_profit": round(gross_profit, 2),
            "total_fees": round(total_fees, 2),
            "fees_pct_of_gross_profit": round(fee_pct, 2) if fee_pct is not None else None,
        },
        "drawdown": {
            "worst_time": worst_equity.ts if worst_equity else None,
            "worst_drawdown_pct": worst_equity.drawdown_pct if worst_equity else None,
        },
        "recommendations": recommendations,
    }))


@router.get("/{run_id}")
async def get_run(run_id: int) -> JSONResponse:
    result = await get_backtest_run(run_id)
    if result is None:
        return JSONResponse({"ok": False, "message": "Backtest run not found."}, status_code=404)
    return JSONResponse(result)


def _csv_response(name: str, rows: list[dict], fields: list[str]) -> StreamingResponse:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row.get(field) for field in fields})
    out.seek(0)
    return StreamingResponse(
        iter([out.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get("/{run_id}/trades.csv")
async def export_trades(run_id: int):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(BacktestTrade).where(BacktestTrade.run_id == run_id).order_by(BacktestTrade.entry_ts.asc()))
        rows = result.scalars().all()
    if not rows:
        return JSONResponse({"ok": False, "message": "No trades found for this run."}, status_code=404)
    fields = [
        "side",
        "entry_ts",
        "exit_ts",
        "entry_px",
        "exit_px",
        "qty",
        "gross_pnl",
        "fees",
        "net_pnl",
        "return_pct",
        "r_multiple",
        "exit_reason",
        "ai_confidence",
        "ai_model",
        "ai_reason",
    ]
    return _csv_response(
        f"backtest_{run_id}_trades.csv",
        [
            {
                "side": r.side,
                "entry_ts": r.entry_ts,
                "exit_ts": r.exit_ts,
                "entry_px": r.entry_px,
                "exit_px": r.exit_px,
                "qty": r.qty,
                "gross_pnl": r.gross_pnl,
                "fees": r.fees,
                "net_pnl": r.net_pnl,
                "return_pct": r.return_pct,
                "r_multiple": r.r_multiple,
                "exit_reason": r.exit_reason,
                "ai_confidence": (r.payload or {}).get("ai_confidence"),
                "ai_model": (r.payload or {}).get("ai_model"),
                "ai_reason": (r.payload or {}).get("ai_reason"),
            }
            for r in rows
        ],
        fields,
    )


@router.get("/{run_id}/equity.csv")
async def export_equity(run_id: int):
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(BacktestEquityPoint).where(BacktestEquityPoint.run_id == run_id).order_by(BacktestEquityPoint.ts.asc()))
        rows = result.scalars().all()
    if not rows:
        return JSONResponse({"ok": False, "message": "No equity points found for this run."}, status_code=404)
    fields = ["ts", "equity", "cash", "position_value", "drawdown_pct"]
    return _csv_response(
        f"backtest_{run_id}_equity.csv",
        [
            {
                "ts": r.ts,
                "equity": r.equity,
                "cash": r.cash,
                "position_value": r.position_value,
                "drawdown_pct": r.drawdown_pct,
            }
            for r in rows
        ],
        fields,
    )
