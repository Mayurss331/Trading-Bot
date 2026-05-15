from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class BacktestConfig:
    pair: str
    market: str
    mode: str = "futures"
    strategy: str = "confluence"
    custom_strategy_id: int | None = None
    timeframe: str = "15m"
    lookback_days: int = 30
    initial_capital: float = 10_000.0
    risk: float = 10.0
    commission_bps: float = 5.0
    spread_bps: float = 0.0
    slippage_bps: float = 0.0
    leverage: float = 1.0
    risk_reward_ratio: float = 2.0
    target_mode: str = "strategy_or_rr"
    allow_shorts: bool = True
    fill_model: str = "next_open"
    position_sizing: str = "risk_fixed"
    risk_mode: str = "fixed_amount"
    opposite_signal_mode: str = "ignore"
    same_bar_priority: str = "stop_first"
    finalize_open_trade: bool = True
    ai_verification_enabled: bool = False
    ai_min_confidence: float = 70.0
    ai_candles: int = 80
    ai_model: str = "gpt-5.4-mini"
    limit: int | None = None

    def normalized(self) -> "BacktestConfig":
        mode = self.mode.lower()
        if mode not in {"spot", "margin", "futures"}:
            mode = "futures"
        fill_model = self.fill_model if self.fill_model in {"next_open", "close"} else "next_open"
        position_sizing = self.position_sizing if self.position_sizing in {"risk_fixed", "cash_fraction", "fixed_qty"} else "risk_fixed"
        risk_mode = self.risk_mode if self.risk_mode in {"fixed_amount", "percent_equity"} else "fixed_amount"
        opposite_signal_mode = self.opposite_signal_mode if self.opposite_signal_mode in {"ignore", "exit_only", "reverse"} else "ignore"
        same_bar_priority = self.same_bar_priority if self.same_bar_priority in {"stop_first", "target_first"} else "stop_first"
        target_mode = self.target_mode if self.target_mode in {"strategy_or_rr", "risk_reward"} else "strategy_or_rr"
        return BacktestConfig(
            pair=(self.pair or "B-ETH_USDT").strip(),
            market=(self.market or "ETHUSDT").strip(),
            mode=mode,
            strategy=(self.strategy or "confluence").strip(),
            custom_strategy_id=self.custom_strategy_id if self.custom_strategy_id and self.custom_strategy_id > 0 else None,
            timeframe=(self.timeframe or "15m").strip(),
            lookback_days=max(1, min(int(self.lookback_days or 30), 365)),
            initial_capital=max(1.0, min(float(self.initial_capital or 10_000), 1_000_000_000.0)),
            risk=max(0.01, min(float(self.risk or 10.0), 1_000_000_000.0)),
            commission_bps=max(0.0, min(float(self.commission_bps or 0.0), 1_000.0)),
            spread_bps=max(0.0, min(float(self.spread_bps or 0.0), 1_000.0)),
            slippage_bps=max(0.0, min(float(self.slippage_bps or 0.0), 1_000.0)),
            leverage=max(1.0, min(float(self.leverage or 1.0), 100.0)),
            risk_reward_ratio=max(0.1, min(float(self.risk_reward_ratio or 2.0), 20.0)),
            target_mode=target_mode,
            allow_shorts=bool(self.allow_shorts and mode in {"margin", "futures"}),
            fill_model=fill_model,
            position_sizing=position_sizing,
            risk_mode=risk_mode,
            opposite_signal_mode=opposite_signal_mode,
            same_bar_priority=same_bar_priority,
            finalize_open_trade=bool(self.finalize_open_trade),
            ai_verification_enabled=bool(self.ai_verification_enabled),
            ai_min_confidence=max(0.0, min(float(self.ai_min_confidence or 70.0), 100.0)),
            ai_candles=max(20, min(int(self.ai_candles or 80), 300)),
            ai_model=(self.ai_model or "gpt-5.4-mini").strip()[:80] or "gpt-5.4-mini",
            limit=max(60, min(int(self.limit), 100_000)) if self.limit else None,
        )

    def to_dict(self) -> dict:
        return asdict(self)
