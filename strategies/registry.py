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
from .base import DEFAULT_CHART_CONFIG, StrategyContext


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


CHART_CONFIGS: dict[str, dict] = {
    arbitrage.META.id:          getattr(arbitrage,         "CHART_CONFIG", DEFAULT_CHART_CONFIG),
    confluence.META.id:         getattr(confluence,        "CHART_CONFIG", DEFAULT_CHART_CONFIG),
    daily_sweep.META.id:        getattr(daily_sweep,       "CHART_CONFIG", DEFAULT_CHART_CONFIG),
    funding_basis.META.id:      getattr(funding_basis,     "CHART_CONFIG", DEFAULT_CHART_CONFIG),
    mean_reversion.META.id:     getattr(mean_reversion,    "CHART_CONFIG", DEFAULT_CHART_CONFIG),
    mixed_consensus.META.id:    getattr(mixed_consensus,   "CHART_CONFIG", DEFAULT_CHART_CONFIG),
    pairs_stat_arb.META.id:     getattr(pairs_stat_arb,    "CHART_CONFIG", DEFAULT_CHART_CONFIG),
    trend_following.META.id:    getattr(trend_following,   "CHART_CONFIG", DEFAULT_CHART_CONFIG),
    volatility_squeeze.META.id: getattr(volatility_squeeze,"CHART_CONFIG", DEFAULT_CHART_CONFIG),
}


def get_strategy(strategy_id: str) -> StrategyFn:
    return STRATEGIES.get(strategy_id, STRATEGIES["confluence"])


def normalize_strategy_id(strategy_id: str) -> str:
    return strategy_id if strategy_id in STRATEGIES else "confluence"


def get_chart_config(strategy_id: str) -> dict:
    return CHART_CONFIGS.get(strategy_id, DEFAULT_CHART_CONFIG)


def list_strategies() -> list[dict]:
    return [
        {
            "id": meta.id,
            "name": meta.name,
            "description": meta.description,
            "chart_label": meta.chart_label,
            "score_label": meta.score_label,
            "chart_config": CHART_CONFIGS.get(meta.id, DEFAULT_CHART_CONFIG),
        }
        for meta in METAS.values()
    ]
