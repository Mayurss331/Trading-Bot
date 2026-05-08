"""Funding / Basis Scanner — SCAN-only strategy.

Surfaces basis and funding-rate conditions between spot and futures
quotes.  No directional orders.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .base import PaperState, StrategyContext, StrategyMeta, base_frame, state_payload


META = StrategyMeta(
    id="funding_basis",
    name="Funding / Basis",
    description="Basis deviation and funding-rate scanner comparing spot and futures quotes.",
    chart_label="Reference Price",
    score_label="Basis bps",
)

BASIS_THRESHOLD = 0.0030  # 30 bps


def _price(row: dict[str, Any] | None) -> float | None:
    """Extract a positive finite price from a ticker dict."""
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


def _funding_rate(ticker: dict[str, Any] | None) -> float | None:
    """Extract and normalise funding rate from a futures ticker."""
    if not ticker:
        return None
    # Percentage form first
    for key in ("fundingRatePct",):
        try:
            val = float(ticker.get(key))
            if np.isfinite(val):
                return val / 100.0
        except Exception:
            pass
    # Decimal form
    for key in ("funding_rate", "fundingRate"):
        try:
            val = float(ticker.get(key))
            if np.isfinite(val):
                return val
        except Exception:
            pass
    return None


def analyze(bars: pd.DataFrame, ctx: StrategyContext) -> dict:
    frame = base_frame(bars)
    frame["st_line"] = frame["Close"]
    notes: list[str] = []

    spot_price = _price(ctx.extras.get("spot_ticker"))
    futures_price = _price(ctx.extras.get("futures_ticker"))
    funding = _funding_rate(ctx.extras.get("futures_ticker"))

    basis = None

    if spot_price and futures_price:
        mid = (spot_price + futures_price) / 2
        basis = (futures_price - spot_price) / mid if mid else 0.0
        basis_bps = basis * 10_000
        frame["score"] = basis_bps

        funding_str = f"funding={funding:+.6f}" if funding is not None else "funding=N/A"

        if abs(basis) >= BASIS_THRESHOLD:
            direction = "futures premium" if basis > 0 else "futures discount"
            notes.append(f"Basis {basis_bps:+.1f} bps ({direction}), {funding_str}.")
            action = {"type": "SCAN", "side": "SPREAD", "label": "Basis deviation detected", "score": basis_bps}
            frame["reason"] = f"Basis {basis_bps:+.1f} bps exceeds ±{BASIS_THRESHOLD * 10_000:.0f} bps threshold."
        else:
            notes.append(f"Basis {basis_bps:+.1f} bps within range, {funding_str}.")
            action = {"type": "WAIT", "side": "FLAT", "label": "No basis signal"}
            frame["reason"] = f"Basis {basis_bps:+.1f} bps within ±{BASIS_THRESHOLD * 10_000:.0f} bps threshold."
    else:
        frame["score"] = 0
        missing = []
        if not spot_price:
            missing.append("spot")
        if not futures_price:
            missing.append("futures")
        notes.append(f"Missing ticker data: {', '.join(missing)}.")
        action = {"type": "WAIT", "side": "FLAT", "label": "Waiting for tickers"}
        frame["reason"] = "Need both spot and futures quotes for basis calculation."

    state = PaperState()
    latest = frame.iloc[-1] if not frame.empty else pd.Series(dtype=object)
    return {
        "meta": META,
        "frame": frame,
        "state": state_payload(state),
        "events": [
            f"Spot={spot_price:,.6f} Futures={futures_price:,.6f} Basis={basis * 10_000:+.1f} bps"
            if spot_price and futures_price and basis is not None
            else "Funding/basis scanner could not build a complete spot/futures quote."
        ],
        "action": action,
        "reason": str(latest.get("reason") or META.description),
        "indicators": {
            "score": latest.get("score"),
            "rsi": latest.get("rsi"),
            "supertrend": latest.get("st_line"),
            "basis_bps": basis * 10_000 if basis is not None else None,
            "funding_rate": funding,
            "spot_price": spot_price,
            "futures_price": futures_price,
        },
        "notes": notes,
    }
