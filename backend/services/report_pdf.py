from __future__ import annotations

import io
from datetime import datetime
from typing import Any


def _fmt_f(v: Any, dp: int = 4) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{dp}f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_pnl(v: Any) -> str:
    if v is None:
        return "—"
    try:
        n = float(v)
        sign = "+" if n >= 0 else ""
        return f"{sign}${n:,.2f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_dt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    try:
        return datetime.fromisoformat(str(v)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(v)


def _compute_stats(trades: list[dict]) -> dict:
    closed = [t for t in trades if t.get("pnl") is not None]
    pnls = [float(t["pnl"]) for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    net_pnl = sum(pnls) if pnls else 0.0
    win_rate = len(wins) / len(pnls) * 100 if pnls else 0.0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None

    equity, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        equity += p
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    durations = []
    for t in closed:
        try:
            if t.get("entry_ts") and t.get("exit_ts"):
                e = datetime.fromisoformat(str(t["entry_ts"])) if not isinstance(t["entry_ts"], datetime) else t["entry_ts"]
                x = datetime.fromisoformat(str(t["exit_ts"])) if not isinstance(t["exit_ts"], datetime) else t["exit_ts"]
                durations.append((x - e).total_seconds() / 60)
        except Exception:
            pass
    avg_dur = sum(durations) / len(durations) if durations else None

    return {
        "total": len(trades),
        "closed": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "net_pnl": net_pnl,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "max_dd": max_dd,
        "avg_dur": avg_dur,
    }


def generate_pdf(trades: list[dict], title: str, generated_at: datetime | None = None) -> bytes:
    from fpdf import FPDF, XPos, YPos

    if generated_at is None:
        generated_at = datetime.utcnow()

    stats = _compute_stats(trades)
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.add_page()

    # ── Header ────────────────────────────────────────────────────────────────
    pdf.set_fill_color(15, 17, 21)
    pdf.rect(0, 0, 210, 28, style="F")
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(200, 205, 212)
    pdf.set_xy(10, 7)
    pdf.cell(0, 8, "CoinDCX Bot Dashboard", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(90, 98, 112)
    pdf.set_x(10)
    pdf.cell(0, 5, title, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_text_color(40, 40, 40)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_xy(10, 30)
    pdf.cell(0, 5, f"Generated: {generated_at.strftime('%Y-%m-%d %H:%M UTC')}   |   Total records: {stats['total']}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.ln(4)

    # ── Summary box ───────────────────────────────────────────────────────────
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(60, 60, 60)
    pdf.set_fill_color(245, 246, 249)
    pdf.cell(0, 6, "  PERFORMANCE SUMMARY", fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)

    col_w = 95
    row_h = 7

    def stat_row(label: str, value: str, x_offset: float = 0) -> None:
        pdf.set_x(10 + x_offset)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(100, 110, 120)
        pdf.cell(38, row_h, label)
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(30, 30, 30)
        pdf.cell(col_w - 38, row_h, value)

    pf_str = f"{stats['profit_factor']:.2f}" if stats['profit_factor'] is not None else "—"
    dur_str = f"{stats['avg_dur']:.0f} min" if stats['avg_dur'] is not None else "—"
    pnl_str = _fmt_pnl(stats['net_pnl'])

    y_start = pdf.get_y()
    stat_row("Total Trades:", str(stats['total']))
    pdf.set_xy(10 + col_w, y_start)
    stat_row("Wins / Losses:", f"{stats['wins']} / {stats['losses']}", col_w)
    pdf.ln(row_h)

    y_start = pdf.get_y()
    stat_row("Net PnL:", pnl_str)
    pdf.set_xy(10 + col_w, y_start)
    stat_row("Win Rate:", f"{stats['win_rate']:.1f}%", col_w)
    pdf.ln(row_h)

    y_start = pdf.get_y()
    stat_row("Profit Factor:", pf_str)
    pdf.set_xy(10 + col_w, y_start)
    stat_row("Max Drawdown:", f"${stats['max_dd']:.2f}", col_w)
    pdf.ln(row_h)

    y_start = pdf.get_y()
    stat_row("Avg Duration:", dur_str)
    pdf.set_xy(10 + col_w, y_start)
    stat_row("Closed Trades:", str(stats['closed']), col_w)
    pdf.ln(row_h + 4)

    if not trades:
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(130, 130, 130)
        pdf.cell(0, 8, "No trades recorded for this execution mode.", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        return pdf.output()

    # ── Trade log table ───────────────────────────────────────────────────────
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(60, 60, 60)
    pdf.set_fill_color(245, 246, 249)
    pdf.cell(0, 6, "  TRADE LOG", fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)

    # Column definitions: (header, width, align)
    cols = [
        ("#",         7,  "C"),
        ("Pair",     28,  "L"),
        ("Side",     12,  "C"),
        ("Mode",     14,  "C"),
        ("Entry Time",30, "L"),
        ("Exit Time", 30, "L"),
        ("Entry Px",  22, "R"),
        ("Exit Px",   22, "R"),
        ("Qty",       16, "R"),
        ("PnL",       18, "R"),
        ("Reason",    14, "C"),
    ]
    total_w = sum(c[1] for c in cols)

    # Header row
    pdf.set_fill_color(30, 35, 45)
    pdf.set_text_color(200, 205, 212)
    pdf.set_font("Helvetica", "B", 7)
    for header, w, align in cols:
        pdf.cell(w, 6, header, border=0, align=align, fill=True)
    pdf.ln(6)

    # Data rows
    for i, t in enumerate(trades):
        if pdf.get_y() > 270:
            pdf.add_page()
            # Re-draw header on new page
            pdf.set_fill_color(30, 35, 45)
            pdf.set_text_color(200, 205, 212)
            pdf.set_font("Helvetica", "B", 7)
            for header, w, align in cols:
                pdf.cell(w, 6, header, border=0, align=align, fill=True)
            pdf.ln(6)

        row_fill = i % 2 == 0
        pdf.set_fill_color(248, 249, 251) if row_fill else pdf.set_fill_color(255, 255, 255)
        pdf.set_font("Helvetica", "", 7)

        side_val = t.get("side", 0)
        side_str = "LONG" if side_val == 1 else "SHORT" if side_val == -1 else "—"
        pnl_val = t.get("pnl")

        # Color PnL
        def cell(w: int, txt: str, align: str = "L", fill: bool = True) -> None:
            pdf.set_text_color(40, 40, 40)
            pdf.cell(w, 5, txt, border=0, align=align, fill=fill)

        def pnl_cell(w: int, txt: str) -> None:
            if pnl_val is None:
                pdf.set_text_color(140, 140, 140)
            elif float(pnl_val) >= 0:
                pdf.set_text_color(17, 135, 93)
            else:
                pdf.set_text_color(191, 52, 52)
            pdf.cell(w, 5, txt, border=0, align="R", fill=row_fill)
            pdf.set_fill_color(248, 249, 251) if row_fill else pdf.set_fill_color(255, 255, 255)

        def side_cell(w: int, txt: str) -> None:
            pdf.set_text_color(17, 135, 93) if txt == "LONG" else pdf.set_text_color(191, 52, 52)
            pdf.cell(w, 5, txt, border=0, align="C", fill=row_fill)
            pdf.set_fill_color(248, 249, 251) if row_fill else pdf.set_fill_color(255, 255, 255)

        pdf.set_text_color(40, 40, 40)
        pdf.cell(cols[0][1], 5, str(i + 1), align="C", fill=row_fill)
        cell(cols[1][1], str(t.get("pair") or "—")[:20])
        side_cell(cols[2][1], side_str)
        pdf.set_text_color(40, 40, 40)
        cell(cols[3][1], str(t.get("execution_mode") or "—")[:8], "C")
        cell(cols[4][1], _fmt_dt(t.get("entry_ts")))
        cell(cols[5][1], _fmt_dt(t.get("exit_ts")))
        cell(cols[6][1], _fmt_f(t.get("entry_px"), 4), "R")
        cell(cols[7][1], _fmt_f(t.get("exit_px"), 4) if t.get("exit_px") is not None else "—", "R")
        cell(cols[8][1], _fmt_f(t.get("qty"), 4), "R")
        pnl_cell(cols[9][1], _fmt_pnl(pnl_val))
        pdf.set_text_color(40, 40, 40)
        cell(cols[10][1], str(t.get("exit_reason") or "—")[:10], "C")
        pdf.ln(5)

    # Footer
    pdf.ln(6)
    pdf.set_draw_color(220, 220, 220)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(3)
    pdf.set_font("Helvetica", "I", 7)
    pdf.set_text_color(160, 160, 160)
    pdf.cell(0, 5, f"CoinDCX Bot Dashboard  |  {title}  |  {generated_at.strftime('%Y-%m-%d %H:%M UTC')}  |  All figures in USDT", align="C")

    return bytes(pdf.output())
