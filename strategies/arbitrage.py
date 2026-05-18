from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .base import StrategyContext, StrategyMeta, base_frame, state_payload, PaperState


META = StrategyMeta(
    id="arbitrage",
    name="Arbitrage Scanner",
    description="Paper-only spread scanner comparing CoinDCX spot and futures quotes.",
    chart_label="Reference Price",
    score_label="Spread bps",
)

CHART_CONFIG = {
    "overlays": [],
    "signals": False,
    "extra_cols": [],
}


def _price(row: dict[str, Any] | None) -> float | None:
    if not row:
        return None
    for key in ("last_price", "last", "close"):
        try:
            value = float(row.get(key))
            if np.isfinite(value) and value > 0:
                return value
        except Exception:
            pass
    return None


def analyze(bars: pd.DataFrame, ctx: StrategyContext) -> dict:
    frame = base_frame(bars)
    frame["st_line"] = frame["Close"]
    spot_price = _price(ctx.extras.get("spot_ticker"))
    futures_price = _price(ctx.extras.get("futures_ticker"))
    spread_bps = None
    notes: list[str] = []

    if spot_price and futures_price:
        mid = (spot_price + futures_price) / 2
        spread_bps = ((futures_price - spot_price) / mid) * 10_000 if mid else 0.0
        frame["score"] = spread_bps
        if abs(spread_bps) >= 20:
            direction = "futures expensive: sell futures / buy spot" if spread_bps > 0 else "spot expensive: buy futures / sell spot"
            notes.append(f"Spread {spread_bps:+.1f} bps, {direction}.")
            action = {"type": "SCAN", "side": "SPREAD", "label": "Arbitrage spread detected"}
        else:
            notes.append(f"Spread {spread_bps:+.1f} bps is below the 20 bps watch threshold.")
            action = {"type": "WAIT", "side": "FLAT", "label": "No arbitrage spread"}
    else:
        frame["score"] = 0
        notes.append("Need both spot and futures quotes for arbitrage comparison.")
        action = {"type": "WAIT", "side": "FLAT", "label": "Waiting for spread data"}

    frame["reason"] = "Compares live spot and futures quotes. Fees, depth, slippage, and latency are not netted yet."
    state = PaperState()
    events = [
        f"Spot={spot_price:,.6f} Futures={futures_price:,.6f} Spread={spread_bps:+.1f} bps"
        if spot_price and futures_price and spread_bps is not None
        else "Arbitrage scanner could not build a complete spot/futures quote."
    ]
    latest = frame.iloc[-1] if not frame.empty else pd.Series(dtype=object)
    return {
        "meta": META,
        "frame": frame,
        "state": state_payload(state),
        "events": events,
        "action": action,
        "reason": str(latest.get("reason") or META.description),
        "indicators": {
            "score": latest.get("score"),
            "rsi": latest.get("rsi"),
            "supertrend": latest.get("st_line"),
            "spread_bps": spread_bps,
            "spot_price": spot_price,
            "futures_price": futures_price,
        },
        "notes": notes,
    }
