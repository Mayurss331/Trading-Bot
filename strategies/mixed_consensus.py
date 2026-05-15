"""Mixed Consensus — meta-strategy voting across child strategies.

Enters a position only when at least two directional strategies agree
on the same side and exits when consensus drops below two votes.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .base import FLAT, LONG, SHORT, StrategyContext, StrategyMeta, base_frame, finalize, zscore


META = StrategyMeta(
    id="mixed_consensus",
    name="Mixed Consensus",
    description="Enters when ≥2 directional strategies agree; exits on consensus break.",
    chart_label="Supertrend",
    score_label="Vote Delta",
)

# Child strategy IDs (must be directional, not scanners)
ELIGIBLE_IDS = ["confluence", "trend_following", "mean_reversion", "volatility_squeeze"]

VOTE_THRESHOLD = 2

# Strategies suited to each regime (by ATR z-score)
_TREND_STRATEGIES = {"confluence", "trend_following", "volatility_squeeze"}
_REVERSION_STRATEGIES = {"mean_reversion"}
_ATR_Z_PERIOD = 20
_REGIME_THRESHOLD = 0.5  # z > +0.5 = high-vol/trending, z < -0.5 = low-vol/mean-reverting


def _get_child_analyzers() -> list[tuple[str, object]]:
    """Lazy-import child analyzers to avoid circular imports."""
    from . import confluence, mean_reversion, trend_following, volatility_squeeze

    return [
        ("confluence", confluence.analyze),
        ("trend_following", trend_following.analyze),
        ("mean_reversion", mean_reversion.analyze),
        ("volatility_squeeze", volatility_squeeze.analyze),
    ]


def _persistent_votes(child_frame: pd.DataFrame) -> pd.Series:
    """Forward-fill entry_side until exit flag is triggered.

    Child strategies only set entry_side on the bar they enter;
    we forward-fill to create a persistent vote series that remains
    active until the child's own exit condition fires.
    """
    entry = child_frame["entry_side"].copy()
    exit_long = child_frame["exit_long"].astype(bool)
    exit_short = child_frame["exit_short"].astype(bool)

    votes = pd.Series(0, index=child_frame.index, dtype=int)
    current = 0
    for i in range(len(entry)):
        es = int(entry.iloc[i])
        if current == LONG and exit_long.iloc[i]:
            current = 0
        elif current == SHORT and exit_short.iloc[i]:
            current = 0

        if es in (LONG, SHORT) and current == FLAT:
            current = es

        votes.iloc[i] = current
    return votes


def analyze(bars: pd.DataFrame, ctx: StrategyContext) -> dict:
    frame = base_frame(bars)
    notes: list[str] = []
    child_analyzers = _get_child_analyzers()

    # ── Collect child vote series ────────────────────────────────────
    all_votes: dict[str, pd.Series] = {}
    sources_ok: list[str] = []

    for name, analyzer_fn in child_analyzers:
        try:
            result = analyzer_fn(bars, ctx)
            child_frame = result.get("frame")
            if child_frame is None or child_frame.empty:
                all_votes[name] = pd.Series(0, index=frame.index, dtype=int)
                notes.append(f"{name}: empty frame, neutral vote.")
            else:
                vote_series = _persistent_votes(child_frame)
                all_votes[name] = vote_series.reindex(frame.index).fillna(0).astype(int)
                sources_ok.append(name)
        except Exception as exc:
            all_votes[name] = pd.Series(0, index=frame.index, dtype=int)
            notes.append(f"{name}: error ({str(exc)[:120]}), neutral vote.")

    # ── Regime detection via ATR z-score ────────────────────────────
    # High-vol regime (z > threshold): favour trend/momentum strategies
    # Low-vol regime (z < -threshold): favour mean-reversion strategies
    atr_z = zscore(frame["atr"], _ATR_Z_PERIOD).fillna(0.0)
    high_vol_regime = atr_z > _REGIME_THRESHOLD   # trending / breakout
    low_vol_regime = atr_z < -_REGIME_THRESHOLD   # mean-reverting / quiet

    # ── Per-bar vote tallying with regime gating ─────────────────────
    vote_df = pd.DataFrame(all_votes, index=frame.index).fillna(0).astype(int)

    # In high-vol regime: suppress reversion votes
    # In low-vol regime: suppress trend votes
    for name in all_votes:
        col = vote_df[name]
        if name in _REVERSION_STRATEGIES:
            vote_df[name] = col.where(~high_vol_regime, other=0)
        elif name in _TREND_STRATEGIES:
            vote_df[name] = col.where(~low_vol_regime, other=0)

    long_votes = (vote_df == LONG).sum(axis=1)
    short_votes = (vote_df == SHORT).sum(axis=1)

    # Suppress SHORT votes if shorts not allowed
    if not ctx.allow_shorts:
        short_votes[:] = 0

    # Entry side per bar (non-persistent; position state handled below)
    entry_side = pd.Series(0, index=frame.index, dtype=int)
    entry_side[(long_votes >= VOTE_THRESHOLD) & (long_votes > short_votes)] = LONG
    entry_side[(short_votes >= VOTE_THRESHOLD) & (short_votes > long_votes)] = SHORT

    # ── Exit flag computation via lightweight position simulation ────
    exit_long_flags = pd.Series(False, index=frame.index)
    exit_short_flags = pd.Series(False, index=frame.index)
    position = FLAT

    for i in range(len(frame)):
        es = int(entry_side.iloc[i])
        lv = int(long_votes.iloc[i])
        sv = int(short_votes.iloc[i])

        if position == FLAT:
            if es in (LONG, SHORT):
                position = es
        elif position == LONG:
            # Exit when long votes drop below threshold
            if lv < VOTE_THRESHOLD:
                exit_long_flags.iloc[i] = True
                position = FLAT
            # Same-bar flip: if SHORT threshold met, exit long, defer short entry
            elif sv >= VOTE_THRESHOLD and sv > lv:
                exit_long_flags.iloc[i] = True
                position = FLAT
                entry_side.iloc[i] = 0  # defer to next bar
        elif position == SHORT:
            if sv < VOTE_THRESHOLD:
                exit_short_flags.iloc[i] = True
                position = FLAT
            elif lv >= VOTE_THRESHOLD and lv > sv:
                exit_short_flags.iloc[i] = True
                position = FLAT
                entry_side.iloc[i] = 0

        # allow_shorts enforcement: if short position opened, exit immediately
        if not ctx.allow_shorts and position == SHORT:
            exit_short_flags.iloc[i] = True
            position = FLAT
            entry_side.iloc[i] = 0

    frame["entry_side"] = entry_side
    frame["exit_long"] = exit_long_flags
    frame["exit_short"] = exit_short_flags

    # Score = long_votes - short_votes
    frame["score"] = long_votes - short_votes

    # Reason per bar (includes regime context)
    source_str = ",".join(sources_ok) if sources_ok else "none"
    regime_labels = []
    for hz, lz in zip(high_vol_regime, low_vol_regime):
        regime_labels.append("trend" if hz else ("reversion" if lz else "neutral"))
    frame["reason"] = [
        f"votes L={lv} S={sv} regime={reg} (sources={source_str})"
        for lv, sv, reg in zip(long_votes, short_votes, regime_labels)
    ]

    # Top-level notes: latest bar summary + any child errors
    latest_lv = int(long_votes.iloc[-1]) if len(long_votes) > 0 else 0
    latest_sv = int(short_votes.iloc[-1]) if len(short_votes) > 0 else 0
    notes.insert(0, f"Latest votes: L={latest_lv} S={latest_sv} from {len(sources_ok)}/{len(ELIGIBLE_IDS)} strategies.")

    return finalize(META, frame, ctx, notes=notes)
