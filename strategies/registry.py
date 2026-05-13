from __future__ import annotations

from collections.abc import Callable

import pandas as pd

from . import (
    arbitrage,
    confluence,
    daily_sweep,
    funding_basis,
    mean_reversion,
    mixed_consensus,
    pairs_stat_arb,
    trend_following,
    volatility_squeeze,
)
from .base import StrategyContext


StrategyFn = Callable[[pd.DataFrame, StrategyContext], dict]

# Sorted alphabetically by strategy ID
STRATEGIES: dict[str, StrategyFn] = {
    arbitrage.META.id: arbitrage.analyze,
    confluence.META.id: confluence.analyze,
    daily_sweep.META.id: daily_sweep.analyze,
    funding_basis.META.id: funding_basis.analyze,
    mean_reversion.META.id: mean_reversion.analyze,
    mixed_consensus.META.id: mixed_consensus.analyze,
    pairs_stat_arb.META.id: pairs_stat_arb.analyze,
    trend_following.META.id: trend_following.analyze,
    volatility_squeeze.META.id: volatility_squeeze.analyze,
}

METAS = {
    arbitrage.META.id: arbitrage.META,
    confluence.META.id: confluence.META,
    daily_sweep.META.id: daily_sweep.META,
    funding_basis.META.id: funding_basis.META,
    mean_reversion.META.id: mean_reversion.META,
    mixed_consensus.META.id: mixed_consensus.META,
    pairs_stat_arb.META.id: pairs_stat_arb.META,
    trend_following.META.id: trend_following.META,
    volatility_squeeze.META.id: volatility_squeeze.META,
}


def get_strategy(strategy_id: str) -> StrategyFn:
    return STRATEGIES.get(strategy_id, STRATEGIES["confluence"])


def normalize_strategy_id(strategy_id: str) -> str:
    return strategy_id if strategy_id in STRATEGIES else "confluence"


def list_strategies() -> list[dict]:
    return [
        {
            "id": meta.id,
            "name": meta.name,
            "description": meta.description,
            "chart_label": meta.chart_label,
            "score_label": meta.score_label,
        }
        for meta in METAS.values()
    ]
