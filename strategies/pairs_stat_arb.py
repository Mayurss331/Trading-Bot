"""Pairs / Stat-Arb Scanner — SCAN-only strategy.

Surfaces mean-reverting pair spreads for manual review using
log price ratio z-scores.  No directional orders.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .base import PaperState, StrategyContext, StrategyMeta, base_frame, state_payload


META = StrategyMeta(
    id="pairs_stat_arb",
    name="Pairs Scanner",
    description="Log-ratio z-score scanner comparing two asset price series.",
    chart_label="Reference Price",
    score_label="Z-Score",
)

Z_THRESHOLD = 2.0
MIN_ALIGNED = 60


def analyze(bars: pd.DataFrame, ctx: StrategyContext) -> dict:
    frame = base_frame(bars)
    frame["st_line"] = frame["Close"]
    notes: list[str] = []

    pair2_bars = ctx.extras.get("pair2_bars")

    # ── Validate secondary series ────────────────────────────────────
    if pair2_bars is None or (isinstance(pair2_bars, pd.DataFrame) and pair2_bars.empty):
        frame["score"] = 0
        frame["reason"] = "Waiting for secondary pair data."
        return _payload(frame, notes=["pair2 missing"],
                        action={"type": "WAIT", "side": "FLAT", "label": "Waiting for pair2"})

    required_cols = {"Open", "High", "Low", "Close"}
    if not required_cols.issubset(set(pair2_bars.columns)):
        frame["score"] = 0
        frame["reason"] = "Secondary pair missing required OHLCV columns."
        return _payload(frame, notes=["pair2 missing columns"],
                        action={"type": "WAIT", "side": "FLAT", "label": "Waiting for pair2"})

    # ── Align by timestamp ───────────────────────────────────────────
    close1 = bars["Close"]
    close2 = pair2_bars["Close"]
    aligned = pd.DataFrame({"c1": close1, "c2": close2}).dropna()

    # Drop non-positive
    aligned = aligned[(aligned["c1"] > 0) & (aligned["c2"] > 0)]

    if len(aligned) < MIN_ALIGNED:
        frame["score"] = 0
        frame["reason"] = f"Insufficient aligned bars ({len(aligned)} < {MIN_ALIGNED})."
        return _payload(frame, notes=[f"Only {len(aligned)} aligned bars, need {MIN_ALIGNED}"],
                        action={"type": "WAIT", "side": "FLAT", "label": "Waiting for pair2 bars"})

    # ── Compute z-score ──────────────────────────────────────────────
    log_ratio = np.log(aligned["c1"]) - np.log(aligned["c2"])
    rolling_mean = log_ratio.rolling(MIN_ALIGNED).mean()
    rolling_std = log_ratio.rolling(MIN_ALIGNED).std()
    z = ((log_ratio - rolling_mean) / rolling_std.replace(0, np.nan)).fillna(0)

    # Map z-score back to primary frame index
    frame["score"] = z.reindex(frame.index).fillna(0)
    latest_z = float(z.iloc[-1]) if len(z) > 0 else 0.0

    if abs(latest_z) >= Z_THRESHOLD:
        direction = "ratio stretched high" if latest_z > 0 else "ratio stretched low"
        notes.append(f"Z-score {latest_z:+.2f}, {direction}.")
        action = {"type": "SCAN", "side": "SPREAD", "label": "Pair spread detected", "score": latest_z}
        frame["reason"] = f"Z-score {latest_z:+.2f} exceeds ±{Z_THRESHOLD} threshold."
    else:
        notes.append(f"Z-score {latest_z:+.2f} is within ±{Z_THRESHOLD} range.")
        action = {"type": "WAIT", "side": "FLAT", "label": "No spread signal"}
        frame["reason"] = f"Z-score {latest_z:+.2f} within normal range."

    return _payload(frame, notes=notes, action=action)


def _payload(frame: pd.DataFrame, notes: list[str], action: dict) -> dict:
    state = PaperState()
    latest = frame.iloc[-1] if not frame.empty else pd.Series(dtype=object)
    return {
        "meta": META,
        "frame": frame,
        "state": state_payload(state),
        "events": [],
        "action": action,
        "reason": str(latest.get("reason") or META.description),
        "indicators": {
            "score": latest.get("score"),
            "rsi": latest.get("rsi"),
            "supertrend": latest.get("st_line"),
        },
        "notes": notes,
    }
