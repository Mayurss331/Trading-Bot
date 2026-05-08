from __future__ import annotations

from collections.abc import Callable

import pandas as pd

from . import arbitrage, confluence, mean_reversion, trend_following
from .base import StrategyContext


StrategyFn = Callable[[pd.DataFrame, StrategyContext], dict]


STRATEGIES: dict[str, StrategyFn] = {
    confluence.META.id: confluence.analyze,
    trend_following.META.id: trend_following.analyze,
    mean_reversion.META.id: mean_reversion.analyze,
    arbitrage.META.id: arbitrage.analyze,
}

METAS = {
    confluence.META.id: confluence.META,
    trend_following.META.id: trend_following.META,
    mean_reversion.META.id: mean_reversion.META,
    arbitrage.META.id: arbitrage.META,
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
