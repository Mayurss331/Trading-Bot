from __future__ import annotations

import asyncio
import csv
import io
import os
from datetime import datetime, timedelta

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select

from ..db.database import AsyncSessionLocal
from ..db.models import (
    AccountSnapshot,
    Candle,
    PaperAccount,
    PaperOrder,
    PositionSnapshot,
    SignalEvent,
    StateSnapshot,
    Trade,
    UserSetting,
)
from ..utils import clean

router = APIRouter(tags=["reports"])


def _trade_row(t: Trade) -> dict:
    return {
        "id": t.id,
        "pair": t.pair,
        "side": t.side,
        "entry_ts": t.entry_ts.isoformat() if t.entry_ts else None,
        "exit_ts": t.exit_ts.isoformat() if t.exit_ts else None,
        "entry_px": t.entry_px,
        "exit_px": t.exit_px,
        "stop_px": t.stop_px,
        "target_px": t.target_px,
        "qty": t.qty,
        "risk_usd": t.risk_usd,
        "pnl": t.pnl,
        "exit_reason": t.exit_reason,
        "mode": t.mode,
        "strategy": t.strategy,
        "execution_mode": t.execution_mode,
    }


@router.get("/api/reports/trades")
async def report_trades(
    exec_mode: str = "all",
    pair: str = "",
    date_from: str = "",
    date_to: str = "",
    limit: int = 100,
    offset: int = 0,
) -> JSONResponse:
    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    filters = []
    if exec_mode in {"paper", "real"}:
        filters.append(Trade.execution_mode == exec_mode)
    if pair:
        filters.append(Trade.pair == pair)
    if date_from:
        try:
            filters.append(Trade.entry_ts >= datetime.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            filters.append(Trade.entry_ts <= datetime.fromisoformat(date_to))
        except ValueError:
            pass
    stmt = select(Trade).where(*filters).order_by(Trade.entry_ts.desc()).limit(limit).offset(offset)
    count_stmt = select(func.count()).select_from(Trade).where(*filters)

    async with AsyncSessionLocal() as db:
        total_result = await db.execute(count_stmt)
        result = await db.execute(stmt)
        total = int(total_result.scalar() or 0)
        trades = result.scalars().all()

    return JSONResponse({
        "ok": True,
        "count": len(trades),
        "total": total,
        "limit": limit,
        "offset": offset,
        "trades": [_trade_row(t) for t in trades],
    })


@router.get("/api/reports/overview")
async def report_overview(exec_mode: str = "all") -> JSONResponse:
    filters = []
    if exec_mode in {"paper", "real"}:
        filters.append(Trade.execution_mode == exec_mode)

    closed_filters = [*filters, Trade.exit_ts.isnot(None)]
    stmt = select(Trade).where(*closed_filters).order_by(Trade.exit_ts.asc(), Trade.entry_ts.asc())
    total_stmt = select(func.count()).select_from(Trade).where(*filters)
    open_stmt = select(func.count()).select_from(Trade).where(*filters, Trade.exit_ts.is_(None))

    async with AsyncSessionLocal() as db:
        total_result = await db.execute(total_stmt)
        open_result = await db.execute(open_stmt)
        result = await db.execute(stmt)
        total_trades = int(total_result.scalar() or 0)
        open_trades = int(open_result.scalar() or 0)
        trades = result.scalars().all()

    if not trades:
        return JSONResponse({
            "ok": True, "exec_mode": exec_mode,
            "total_trades": total_trades,
            "closed_trades": 0,
            "open_trades": open_trades,
            "wins": 0, "losses": 0, "win_rate": None,
            "net_pnl": 0.0, "profit_factor": None, "max_drawdown": None,
            "avg_duration_minutes": None,
        })

    pnls = [t.pnl for t in trades if t.pnl is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    net_pnl = sum(pnls)
    win_rate = len(wins) / len(pnls) * 100 if pnls else None
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

    # Max drawdown from equity curve
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    durations = []
    for t in trades:
        if t.entry_ts and t.exit_ts:
            durations.append((t.exit_ts - t.entry_ts).total_seconds() / 60)
    avg_duration = sum(durations) / len(durations) if durations else None

    return JSONResponse(clean({
        "ok": True,
        "exec_mode": exec_mode,
        "total_trades": total_trades,
        "closed_trades": len(trades),
        "open_trades": open_trades,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "net_pnl": net_pnl,
        "profit_factor": profit_factor,
        "max_drawdown": max_dd if max_dd > 0 else 0.0,
        "avg_duration_minutes": avg_duration,
    }))


@router.get("/api/reports/pnl-series")
async def report_pnl_series(exec_mode: str = "all") -> JSONResponse:
    stmt = select(Trade).where(Trade.exit_ts.isnot(None), Trade.pnl.isnot(None))
    if exec_mode in {"paper", "real"}:
        stmt = stmt.where(Trade.execution_mode == exec_mode)
    stmt = stmt.order_by(Trade.exit_ts.asc())

    async with AsyncSessionLocal() as db:
        result = await db.execute(stmt)
        trades = result.scalars().all()

    cumulative = 0.0
    series: list[dict] = []
    for t in trades:
        cumulative += t.pnl or 0.0
        date_str = t.exit_ts.strftime("%Y-%m-%d") if t.exit_ts else None
        if series and series[-1]["date"] == date_str:
            series[-1]["cumulative_pnl"] = cumulative
            series[-1]["daily_pnl"] += t.pnl or 0.0
        else:
            series.append({
                "date": date_str,
                "daily_pnl": t.pnl or 0.0,
                "cumulative_pnl": cumulative,
            })

    return JSONResponse({"ok": True, "exec_mode": exec_mode, "series": series})


@router.get("/api/reports/paper-trading")
async def paper_trading_overview(month: str = "", limit: int = 80) -> JSONResponse:
    month_key = month.strip() or datetime.utcnow().strftime("%Y-%m")
    limit = max(1, min(limit, 300))
    async with AsyncSessionLocal() as db:
        account_result = await db.execute(
            select(PaperAccount)
            .where(PaperAccount.month_key == month_key)
            .order_by(PaperAccount.equity.desc())
        )
        accounts = account_result.scalars().all()
        account_ids = [a.id for a in accounts]
        if account_ids:
            order_result = await db.execute(
                select(PaperOrder)
                .where(PaperOrder.account_id.in_(account_ids))
                .order_by(PaperOrder.updated_at.desc(), PaperOrder.entry_ts.desc())
                .limit(limit)
            )
            orders = order_result.scalars().all()
        else:
            orders = []

    account_payloads = []
    for a in accounts:
        account_payloads.append({
            "id": a.id,
            "month_key": a.month_key,
            "strategy_key": a.strategy_key,
            "strategy": a.strategy,
            "custom_strategy_id": a.custom_strategy_id,
            "starting_capital": a.starting_capital,
            "realized_pnl": a.realized_pnl,
            "unrealized_pnl": a.unrealized_pnl,
            "equity": a.equity,
            "return_pct": ((a.equity / a.starting_capital - 1.0) * 100) if a.starting_capital else None,
            "open_positions": a.open_positions,
            "closed_trades": a.closed_trades,
        })

    order_payloads = []
    for o in orders:
        order_payloads.append({
            "id": o.id,
            "account_id": o.account_id,
            "pair": o.pair,
            "coin": o.coin,
            "strategy": o.strategy,
            "custom_strategy_id": o.custom_strategy_id,
            "timeframe": o.timeframe,
            "side": "LONG" if o.side > 0 else "SHORT",
            "status": o.status,
            "entry_ts": o.entry_ts,
            "exit_ts": o.exit_ts,
            "entry_px": o.entry_px,
            "exit_px": o.exit_px,
            "stop_px": o.stop_px,
            "target_px": o.target_px,
            "qty": o.qty,
            "risk_usd": o.risk_usd,
            "realized_pnl": o.realized_pnl,
            "unrealized_pnl": o.unrealized_pnl,
            "exit_reason": o.exit_reason,
            "updated_at": o.updated_at,
        })

    return JSONResponse(clean({
        "ok": True,
        "month_key": month_key,
        "starting_capital_default": _env_float("PAPER_TRADING_MONTHLY_CAPITAL", 100.0),
        "accounts": account_payloads,
        "orders": order_payloads,
    }))


class SendEmailRequest(BaseModel):
    to: str = ""


class SendPaperEvaluationRequest(BaseModel):
    to: str = ""
    strategies: list[str] = Field(default_factory=list)


class ClearHistoryRequest(BaseModel):
    confirm: str


def _trade_dicts(trades) -> list[dict]:
    return [_trade_row(t) for t in trades]


def _parse_emails(raw: str) -> list[str]:
    return [addr.strip() for addr in raw.split(",") if addr.strip() and "@" in addr.strip()]


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


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _paper_eval_recipients() -> list[str]:
    raw = (
        os.getenv("PAPER_TRADING_EMAIL_TO", "").strip()
        or os.getenv("PAPER_EVAL_EMAIL_TO", "").strip()
        or os.getenv("REPORT_EMAIL_TO", "")
    )
    return _parse_emails(raw)


def _paper_eval_strategy_selections(strategies: list[str] | None = None) -> list[str]:
    if strategies:
        selected = [str(item).strip() for item in strategies if str(item).strip()]
    else:
        raw = os.getenv(
            "PAPER_EVAL_STRATEGIES",
            "builtin:confluence,builtin:trend_following,builtin:mean_reversion,builtin:volume_profile",
        )
        selected = [item.strip() for item in raw.split(",") if item.strip()]
    return selected[:20]


async def _dashboard_paper_strategies() -> list[str]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(UserSetting).where(UserSetting.key == "dashboard"))
        setting = result.scalar_one_or_none()
    payload = setting.payload if setting and isinstance(setting.payload, dict) else {}
    selected = payload.get("paperStrategies")
    if not isinstance(selected, list):
        return []
    return [str(item).strip() for item in selected if str(item).strip()][:20]


def _paper_eval_base_payload() -> dict:
    return {
        "pair": os.getenv("PAPER_EVAL_PAIR", os.getenv("DEFAULT_PAIR", "B-BTC_USDT")),
        "market": os.getenv("PAPER_EVAL_MARKET", os.getenv("DEFAULT_MARKET", "BTCUSDT")),
        "mode": os.getenv("PAPER_EVAL_MODE", os.getenv("DEFAULT_MODE", "futures")),
        "strategy": "confluence",
        "timeframe": os.getenv("PAPER_EVAL_TIMEFRAME", "15m"),
        "lookback_days": _env_int("PAPER_EVAL_LOOKBACK_DAYS", _env_int("DEFAULT_LOOKBACK_DAYS", 10)),
        "initial_capital": _env_float("PAPER_EVAL_INITIAL_CAPITAL", 10_000.0),
        "risk": _env_float("PAPER_EVAL_RISK", _env_float("DEFAULT_RISK", 10.0)),
        "commission_bps": _env_float("PAPER_EVAL_COMMISSION_BPS", 5.0),
        "spread_bps": _env_float("PAPER_EVAL_SPREAD_BPS", 0.0),
        "slippage_bps": _env_float("PAPER_EVAL_SLIPPAGE_BPS", 0.0),
        "leverage": _env_float("PAPER_EVAL_LEVERAGE", _env_float("LEVERAGE", 1.0)),
        "risk_reward_ratio": _env_float("PAPER_EVAL_RISK_REWARD_RATIO", _env_float("RISK_REWARD_RATIO", 2.0)),
        "target_mode": os.getenv("PAPER_EVAL_TARGET_MODE", "strategy_or_rr"),
        "allow_shorts": _env_bool("PAPER_EVAL_ALLOW_SHORTS", True),
        "fill_model": os.getenv("PAPER_EVAL_FILL_MODEL", "next_open"),
        "position_sizing": os.getenv("PAPER_EVAL_POSITION_SIZING", "risk_fixed"),
        "risk_mode": os.getenv("PAPER_EVAL_RISK_MODE", "fixed_amount"),
        "opposite_signal_mode": os.getenv("PAPER_EVAL_OPPOSITE_SIGNAL_MODE", "ignore"),
        "same_bar_priority": os.getenv("PAPER_EVAL_SAME_BAR_PRIORITY", "stop_first"),
        "finalize_open_trade": _env_bool("PAPER_EVAL_FINALIZE_OPEN_TRADE", True),
        "warmup_bars": _env_int("PAPER_EVAL_WARMUP_BARS", 50),
        "min_signal_score": _env_float("PAPER_EVAL_MIN_SIGNAL_SCORE", 0.0),
        "ai_verification_enabled": _env_bool("PAPER_EVAL_AI_VERIFICATION_ENABLED", False),
        "ai_min_confidence": _env_float("PAPER_EVAL_AI_MIN_CONFIDENCE", 70.0),
        "ai_candles": _env_int("PAPER_EVAL_AI_CANDLES", 80),
        "ai_model": os.getenv("PAPER_EVAL_AI_MODEL", os.getenv("OPENAI_BACKTEST_MODEL", "gpt-5.4-mini")),
    }


def _paper_eval_payload_for_selection(base_payload: dict, selection: str) -> tuple[dict | None, str | None]:
    kind, _, raw_id = selection.partition(":")
    if not raw_id:
        kind, raw_id = "builtin", kind
    payload = dict(base_payload)
    if kind == "custom":
        try:
            custom_id = int(raw_id)
        except (TypeError, ValueError):
            return None, "Invalid custom strategy id."
        payload["custom_strategy_id"] = custom_id
        payload["strategy"] = "custom"
    else:
        payload["custom_strategy_id"] = None
        payload["strategy"] = raw_id
    return payload, None


def _fmt_metric(value, suffix: str = "", empty: str = "n/a") -> str:
    if value is None:
        return empty
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:.2f}{suffix}"


def _latest_order_text(trade: dict | None) -> str:
    if not trade:
        return "No order"
    side = str(trade.get("side") or "").upper() or "TRADE"
    entry = _fmt_metric(trade.get("entry_px"))
    pnl = _fmt_metric(trade.get("net_pnl"), " USDT")
    reason = trade.get("exit_reason") or ("open" if not trade.get("exit_ts") else "closed")
    return f"{side} @ {entry}, PnL {pnl}, {reason}"


def _paper_eval_csv(results: list[dict]) -> bytes:
    output = io.StringIO()
    fields = [
        "selection", "ok", "run_id", "strategy", "total_return_pct", "sharpe",
        "max_drawdown_pct", "trades", "win_rate_pct", "net_pnl", "latest_order", "message",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for item in results:
        summary = item.get("summary") or {}
        latest = item.get("latest_order") if isinstance(item.get("latest_order"), dict) else None
        writer.writerow({
            "selection": item.get("selection"),
            "ok": bool(item.get("ok")),
            "run_id": item.get("run_id"),
            "strategy": item.get("strategy"),
            "total_return_pct": summary.get("total_return_pct"),
            "sharpe": summary.get("portfolio_sharpe", summary.get("sharpe")),
            "max_drawdown_pct": summary.get("max_drawdown_pct"),
            "trades": summary.get("trades"),
            "win_rate_pct": summary.get("win_rate_pct"),
            "net_pnl": summary.get("net_pnl"),
            "latest_order": _latest_order_text(latest),
            "message": item.get("message", ""),
        })
    return output.getvalue().encode("utf-8")


async def dispatch_report(to_emails: list[str]) -> dict:
    """Core report dispatch — shared by the API endpoint and the daily scheduler."""
    now = datetime.utcnow()

    async with AsyncSessionLocal() as db:
        paper_result = await db.execute(
            select(Trade).where(Trade.execution_mode == "paper").order_by(Trade.entry_ts.asc())
        )
        paper_trades_orm = paper_result.scalars().all()
        real_result = await db.execute(
            select(Trade).where(Trade.execution_mode == "real").order_by(Trade.entry_ts.asc())
        )
        real_trades_orm = real_result.scalars().all()

    paper_dicts = _trade_dicts(paper_trades_orm)
    real_dicts = _trade_dicts(real_trades_orm)

    def _build_and_send():
        from ..services.report_pdf import generate_pdf
        from ..services.email_sender import send_report_email
        p_pdf = generate_pdf(paper_dicts, "Paper Trade Report", now) if paper_dicts else None
        r_pdf = generate_pdf(real_dicts, "Real Trade Report", now) if real_dicts else None
        send_report_email(
            to_emails=to_emails,
            paper_pdf=p_pdf,
            real_pdf=r_pdf,
            paper_count=len(paper_dicts),
            real_count=len(real_dicts),
            generated_at=now,
        )
        return p_pdf, r_pdf

    paper_pdf, real_pdf = await asyncio.to_thread(_build_and_send)

    attachments = []
    if paper_pdf:
        attachments.append("paper_trades.pdf")
    if real_pdf:
        attachments.append("real_trades.pdf")

    recipients = ", ".join(to_emails)
    msg = f"Report sent to {recipients}."
    if attachments:
        msg += f" Attached: {', '.join(attachments)}."
    else:
        msg += " No trades recorded yet — summary sent in email body."

    return {"ok": True, "message": msg, "paper_count": len(paper_dicts), "real_count": len(real_dicts)}


async def dispatch_paper_evaluation_report(
    to_emails: list[str] | None = None,
    strategies: list[str] | None = None,
) -> dict:
    """Run the configured paper strategy set and email separate results per strategy."""
    from backend.backtesting.config import BacktestConfig
    from backend.backtesting.service import run_and_store_backtest
    from ..services.email_sender import send_email

    recipients = to_emails or _paper_eval_recipients()
    if not recipients:
        raise RuntimeError("PAPER_EVAL_EMAIL_TO or REPORT_EMAIL_TO must contain at least one email address.")

    selections = _paper_eval_strategy_selections(strategies or await _dashboard_paper_strategies())
    if not selections:
        raise RuntimeError("No paper evaluation strategies configured.")

    generated_at = datetime.utcnow()
    base_payload = _paper_eval_base_payload()
    results: list[dict] = []

    for selection in selections:
        payload, error = _paper_eval_payload_for_selection(base_payload, selection)
        if error:
            results.append({"ok": False, "selection": selection, "message": error})
            continue
        cfg = BacktestConfig(**payload).normalized()
        try:
            result = await run_and_store_backtest(cfg)
        except Exception as exc:
            results.append({"ok": False, "selection": selection, "message": str(exc)})
            continue
        if not result.get("ok"):
            results.append({"ok": False, "selection": selection, "message": result.get("message", "Paper evaluation failed.")})
            continue
        trades = result.get("trades") or []
        results.append({
            "ok": True,
            "selection": selection,
            "run_id": result.get("run_id"),
            "strategy": result.get("strategy"),
            "summary": result.get("summary") or {},
            "latest_order": trades[-1] if trades else None,
        })

    successful = [item for item in results if item.get("ok")]
    failed = [item for item in results if not item.get("ok")]
    date_str = generated_at.strftime("%Y-%m-%d_%H%M")
    title = f"Paper Strategy Evaluation - {base_payload['market']} {base_payload['timeframe']}"
    lines = [
        "CoinDCX Bot - Paper Strategy Evaluation",
        f"Generated: {generated_at.strftime('%Y-%m-%d %H:%M UTC')}",
        f"Market: {base_payload['market']} ({base_payload['pair']})",
        f"Mode/timeframe/lookback: {base_payload['mode']} / {base_payload['timeframe']} / {base_payload['lookback_days']} day(s)",
        f"Strategies: {len(results)} checked, {len(successful)} passed, {len(failed)} failed",
        "",
        "RESULTS",
        "-------",
    ]
    for item in results:
        if not item.get("ok"):
            lines.append(f"- {item.get('selection')}: FAILED - {item.get('message')}")
            continue
        summary = item.get("summary") or {}
        latest = item.get("latest_order") if isinstance(item.get("latest_order"), dict) else None
        lines.append(
            "- "
            f"{item.get('selection')} (Run #{item.get('run_id')}): "
            f"Return {_fmt_metric(summary.get('total_return_pct'), '%')}, "
            f"Sharpe {_fmt_metric(summary.get('portfolio_sharpe', summary.get('sharpe')))}, "
            f"DD {_fmt_metric(summary.get('max_drawdown_pct'), '%')}, "
            f"Trades {summary.get('trades', 0)}, "
            f"Win {_fmt_metric(summary.get('win_rate_pct'), '%')}; "
            f"Latest: {_latest_order_text(latest)}"
        )
        warnings = summary.get("assumption_warnings") or []
        if warnings:
            lines.append(f"  Warnings: {'; '.join(str(w) for w in warnings[:3])}")
    lines.extend([
        "",
        "CSV attachment includes the same strategy-by-strategy metrics for filtering or sharing.",
        "",
        "CoinDCX Bot Dashboard",
    ])

    await asyncio.to_thread(
        send_email,
        to_emails=recipients,
        subject=title,
        text_content="\n".join(lines),
        attachments=[{
            "name": f"paper_strategy_evaluation_{date_str}.csv",
            "content": _paper_eval_csv(results),
        }],
    )

    return {
        "ok": True,
        "message": f"Paper strategy evaluation sent to {', '.join(recipients)}.",
        "count": len(results),
        "successful": len(successful),
        "failed": len(failed),
        "results": results,
    }


async def dispatch_paper_trading_report(to_emails: list[str] | None = None, hours: int | None = None) -> dict:
    """Email live paper-trading orders grouped by strategy."""
    from ..services.email_sender import send_email

    recipients = to_emails or _paper_eval_recipients()
    if not recipients:
        raise RuntimeError("PAPER_EVAL_EMAIL_TO or REPORT_EMAIL_TO must contain at least one email address.")

    interval_hours = max(
        1,
        int(hours or _env_int("PAPER_TRADING_EMAIL_INTERVAL_HOURS", _env_int("PAPER_EVAL_EMAIL_INTERVAL_HOURS", 12))),
    )
    generated_at = datetime.utcnow()
    since = generated_at - timedelta(hours=interval_hours)
    month_key = generated_at.strftime("%Y-%m")

    async with AsyncSessionLocal() as db:
        account_result = await db.execute(
            select(PaperAccount)
            .where(
                PaperAccount.month_key == month_key,
            )
            .order_by(PaperAccount.strategy.asc())
        )
        accounts = account_result.scalars().all()
        account_ids = [a.id for a in accounts]
        if account_ids:
            order_result = await db.execute(
                select(PaperOrder)
                .where(
                    PaperOrder.account_id.in_(account_ids),
                    or_(
                        PaperOrder.entry_ts >= since,
                        PaperOrder.exit_ts >= since,
                        PaperOrder.status == "open",
                    ),
                )
                .order_by(PaperOrder.strategy.asc(), PaperOrder.entry_ts.asc())
            )
            orders = order_result.scalars().all()
        else:
            orders = []

    account_by_id = {a.id: a for a in accounts}
    grouped: dict[int, list[PaperOrder]] = {}
    for order in orders:
        grouped.setdefault(order.account_id, []).append(order)

    lines = [
        "CoinDCX Bot - Live Paper Trading Report",
        f"Generated: {generated_at.strftime('%Y-%m-%d %H:%M UTC')}",
        f"Month account: {month_key}",
        f"Window: last {interval_hours} hour(s), plus open positions",
        f"Starting capital: {_fmt_metric(_env_float('PAPER_TRADING_MONTHLY_CAPITAL', 100.0), ' USDT')} per strategy per month",
        f"Paper orders in report: {len(orders)}",
        "",
        "BY STRATEGY",
        "-----------",
    ]
    for account in accounts:
        rows = grouped.get(account.id, [])
        closed = [t for t in rows if t.status == "closed"]
        open_rows = [t for t in rows if t.status == "open"]
        pnl = sum(float(t.realized_pnl or 0.0) for t in closed)
        wins = sum(1 for t in closed if (t.realized_pnl or 0) > 0)
        win_rate = (wins / len(closed) * 100) if closed else None
        lines.append(
            f"- {account.strategy_key}: equity {_fmt_metric(account.equity, ' USDT')}, "
            f"realized {_fmt_metric(account.realized_pnl, ' USDT')}, "
            f"open {account.open_positions}, closed {account.closed_trades}, "
            f"window orders {len(rows)}, win {_fmt_metric(win_rate, '%')}"
        )
        for t in rows[-8:]:
            status = "OPEN" if t.status == "open" else f"CLOSED {t.exit_reason or ''}".strip()
            side = "LONG" if t.side > 0 else "SHORT"
            lines.append(
                f"  #{t.id} {t.pair} {side} {status} "
                f"entry {_fmt_metric(t.entry_px)} exit {_fmt_metric(t.exit_px)} "
                f"pnl {_fmt_metric(t.realized_pnl, ' USDT')}"
            )
    if not accounts:
        lines.append("- No monthly paper accounts exist yet. Start tracker with paper strategies selected.")
    lines.extend(["", "CoinDCX Bot Dashboard"])

    csv_output = io.StringIO()
    writer = csv.DictWriter(csv_output, fieldnames=[
        "account", "account_equity", "id", "strategy", "pair", "side", "status",
        "entry_ts", "exit_ts", "entry_px", "exit_px", "qty", "risk_usd",
        "realized_pnl", "exit_reason",
    ])
    writer.writeheader()
    for t in orders:
        account = account_by_id.get(t.account_id)
        writer.writerow({
            "account": account.strategy_key if account else "",
            "account_equity": account.equity if account else "",
            "id": t.id,
            "strategy": t.strategy,
            "pair": t.pair,
            "side": "LONG" if t.side > 0 else "SHORT",
            "status": "open" if t.exit_ts is None else "closed",
            "entry_ts": t.entry_ts.isoformat() if t.entry_ts else "",
            "exit_ts": t.exit_ts.isoformat() if t.exit_ts else "",
            "entry_px": t.entry_px,
            "exit_px": t.exit_px,
            "qty": t.qty,
            "risk_usd": t.risk_usd,
            "realized_pnl": t.realized_pnl,
            "exit_reason": t.exit_reason,
        })

    await asyncio.to_thread(
        send_email,
        to_emails=recipients,
        subject=f"CoinDCX Live Paper Report - {generated_at.strftime('%Y-%m-%d %H:%M UTC')}",
        text_content="\n".join(lines),
        attachments=[{
            "name": f"live_paper_trades_{generated_at.strftime('%Y-%m-%d_%H%M')}.csv",
            "content": csv_output.getvalue().encode("utf-8"),
        }],
    )

    return {
        "ok": True,
        "message": f"Live paper trading report sent to {', '.join(recipients)}.",
        "count": len(orders),
        "strategies": {account.strategy_key: len(grouped.get(account.id, [])) for account in accounts},
    }


@router.post("/api/reports/clear-history")
async def clear_history(body: ClearHistoryRequest) -> JSONResponse:
    if body.confirm.strip() != "CLEAR HISTORY":
        return JSONResponse({
            "ok": False,
            "message": "Type CLEAR HISTORY to confirm.",
        }, status_code=400)

    from ..bot_loader import bot

    discarded_pending = len(bot.drain_completed_trades())
    counts: dict[str, int] = {}
    async with AsyncSessionLocal() as db:
        for name, model in [
            ("position_snapshots", PositionSnapshot),
            ("account_snapshots", AccountSnapshot),
            ("signal_events", SignalEvent),
            ("snapshots", StateSnapshot),
            ("candles_5m", Candle),
            ("trades", Trade),
        ]:
            count_result = await db.execute(select(func.count()).select_from(model))
            counts[name] = int(count_result.scalar() or 0)
            await db.execute(delete(model))
        await db.commit()

    total = sum(counts.values())
    return JSONResponse({
        "ok": True,
        "message": f"Cleared {total} history row(s).",
        "deleted": counts,
        "discarded_pending_trades": discarded_pending,
    })


@router.post("/api/reports/send-email")
async def send_email_report(body: SendEmailRequest) -> JSONResponse:
    to_emails = _parse_emails(body.to)
    if not to_emails:
        return JSONResponse({"ok": False, "message": "No valid email address provided."}, status_code=400)
    try:
        result = await dispatch_report(to_emails)
        return JSONResponse(result)
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=500)
    except Exception as exc:
        return JSONResponse({"ok": False, "message": f"Failed: {exc}"}, status_code=500)


@router.post("/api/reports/send-paper-evaluation")
async def send_paper_evaluation_report(body: SendPaperEvaluationRequest) -> JSONResponse:
    to_emails = _parse_emails(body.to) if body.to else _paper_eval_recipients()
    if not to_emails:
        return JSONResponse({"ok": False, "message": "No valid email address configured."}, status_code=400)
    try:
        result = await dispatch_paper_evaluation_report(to_emails, body.strategies)
        return JSONResponse(clean(result))
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=500)
    except Exception as exc:
        return JSONResponse({"ok": False, "message": f"Failed: {exc}"}, status_code=500)


@router.post("/api/reports/send-paper-trading")
async def send_paper_trading_report(body: SendEmailRequest) -> JSONResponse:
    to_emails = _parse_emails(body.to) if body.to else _paper_eval_recipients()
    if not to_emails:
        return JSONResponse({"ok": False, "message": "No valid email address configured."}, status_code=400)
    try:
        result = await dispatch_paper_trading_report(to_emails)
        return JSONResponse(clean(result))
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=500)
    except Exception as exc:
        return JSONResponse({"ok": False, "message": f"Failed: {exc}"}, status_code=500)
