from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint

from .database import Base


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    pair = Column(String, nullable=False)
    side = Column(Integer, nullable=False)  # +1 long, -1 short
    entry_ts = Column(DateTime, nullable=False)
    exit_ts = Column(DateTime, nullable=True)
    entry_px = Column(Float, nullable=False)
    exit_px = Column(Float, nullable=True)
    stop_px = Column(Float, nullable=False)
    target_px = Column(Float, nullable=False)
    qty = Column(Float, nullable=False)
    risk_usd = Column(Float, nullable=False)
    pnl = Column(Float, nullable=True)
    exit_reason = Column(String, nullable=True)  # STOP / TARGET / SIGNAL
    mode = Column(String, nullable=True)           # spot / margin / futures
    strategy = Column(String, nullable=True)
    execution_mode = Column(String, nullable=False, default="paper")  # paper / real


class Candle(Base):
    __tablename__ = "candles_5m"

    id = Column(Integer, primary_key=True, autoincrement=True)
    pair = Column(String, nullable=False, index=True)
    ts = Column(DateTime, nullable=False, index=True)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    volume = Column(Float)

    __table_args__ = (UniqueConstraint("pair", "ts", name="uq_candle_pair_ts"),)


class StateSnapshot(Base):
    __tablename__ = "snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime, default=datetime.utcnow, nullable=False)
    pair = Column(String, nullable=False)
    payload = Column(JSON, nullable=False)


class UserSetting(Base):
    __tablename__ = "user_settings"

    key = Column(String, primary_key=True)
    payload = Column(JSON, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class PaperAccount(Base):
    __tablename__ = "paper_accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    month_key = Column(String, nullable=False, index=True)
    strategy_key = Column(String, nullable=False, index=True)
    strategy = Column(String, nullable=False, index=True)
    custom_strategy_id = Column(Integer, ForeignKey("custom_strategies.id"), nullable=True, index=True)
    starting_capital = Column(Float, nullable=False, default=100.0)
    realized_pnl = Column(Float, nullable=False, default=0.0)
    unrealized_pnl = Column(Float, nullable=False, default=0.0)
    equity = Column(Float, nullable=False, default=100.0)
    open_positions = Column(Integer, nullable=False, default=0)
    closed_trades = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("month_key", "strategy_key", name="uq_paper_account_month_strategy"),)


class PaperOrder(Base):
    __tablename__ = "paper_orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("paper_accounts.id"), nullable=False, index=True)
    pair = Column(String, nullable=False, index=True)
    market = Column(String, nullable=True)
    coin = Column(String, nullable=True, index=True)
    strategy = Column(String, nullable=False, index=True)
    custom_strategy_id = Column(Integer, ForeignKey("custom_strategies.id"), nullable=True, index=True)
    timeframe = Column(String, nullable=True)
    side = Column(Integer, nullable=False)
    status = Column(String, nullable=False, default="open", index=True)
    entry_ts = Column(DateTime, nullable=False, index=True)
    exit_ts = Column(DateTime, nullable=True, index=True)
    entry_px = Column(Float, nullable=False)
    exit_px = Column(Float, nullable=True)
    stop_px = Column(Float, nullable=True)
    target_px = Column(Float, nullable=True)
    qty = Column(Float, nullable=False, default=0.0)
    risk_usd = Column(Float, nullable=False, default=0.0)
    realized_pnl = Column(Float, nullable=False, default=0.0)
    unrealized_pnl = Column(Float, nullable=False, default=0.0)
    exit_reason = Column(String, nullable=True)
    payload = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("account_id", "pair", "side", "entry_ts", name="uq_paper_order_account_pair_entry"),)


class PaperOrderEvent(Base):
    __tablename__ = "paper_order_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(Integer, ForeignKey("paper_orders.id"), nullable=True, index=True)
    account_id = Column(Integer, ForeignKey("paper_accounts.id"), nullable=False, index=True)
    ts = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    event_type = Column(String, nullable=False, index=True)
    message = Column(Text, nullable=True)
    payload = Column(JSON, nullable=False, default=dict)


class SignalEvent(Base):
    __tablename__ = "signal_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    pair = Column(String, nullable=False, index=True)
    market = Column(String, nullable=True)
    coin = Column(String, nullable=True, index=True)
    strategy = Column(String, nullable=True, index=True)
    mode = Column(String, nullable=True)
    price = Column(Float, nullable=True)
    score = Column(Float, nullable=True)
    rsi = Column(Float, nullable=True)
    action_type = Column(String, nullable=True)
    side = Column(String, nullable=True)
    signal = Column(String, nullable=True)
    reason = Column(String, nullable=True)
    freshness_minutes = Column(Float, nullable=True)
    payload = Column(JSON, nullable=False)


class AccountSnapshot(Base):
    __tablename__ = "account_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    mode = Column(String, nullable=False)
    currency = Column(String, nullable=True)
    available_quote_balance = Column(Float, nullable=True)
    total_unrealized_pnl = Column(Float, nullable=True)
    positions_count = Column(Integer, nullable=False, default=0)
    payload = Column(JSON, nullable=False)


class PositionSnapshot(Base):
    __tablename__ = "position_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_snapshot_id = Column(Integer, ForeignKey("account_snapshots.id"), nullable=False, index=True)
    pair = Column(String, nullable=True, index=True)
    coin = Column(String, nullable=True, index=True)
    side = Column(String, nullable=True)
    quantity = Column(Float, nullable=True)
    avg_price = Column(Float, nullable=True)
    mark_price = Column(Float, nullable=True)
    liquidation_price = Column(Float, nullable=True)
    stop_loss_trigger = Column(Float, nullable=True)
    take_profit_trigger = Column(Float, nullable=True)
    unrealized_pnl = Column(Float, nullable=True)
    margin = Column(Float, nullable=True)
    leverage = Column(Float, nullable=True)
    payload = Column(JSON, nullable=False)


class CustomStrategy(Base):
    __tablename__ = "custom_strategies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String, nullable=False)
    slug = Column(String, nullable=False, unique=True, index=True)
    description = Column(Text, nullable=True)
    code = Column(Text, nullable=False)
    version = Column(Integer, nullable=False, default=1)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    last_validated_at = Column(DateTime, nullable=True)
    validation_status = Column(String, nullable=True)
    validation_message = Column(Text, nullable=True)


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    pair = Column(String, nullable=False, index=True)
    market = Column(String, nullable=True)
    mode = Column(String, nullable=False)
    timeframe = Column(String, nullable=False)
    strategy = Column(String, nullable=True, index=True)
    custom_strategy_id = Column(Integer, ForeignKey("custom_strategies.id"), nullable=True, index=True)
    custom_strategy_title = Column(String, nullable=True)
    custom_strategy_version = Column(Integer, nullable=True)
    custom_strategy_code_snapshot = Column(Text, nullable=True)
    data_source = Column(String, nullable=True)
    start_ts = Column(DateTime, nullable=True)
    end_ts = Column(DateTime, nullable=True)
    bars_count = Column(Integer, nullable=False, default=0)
    config = Column(JSON, nullable=False)
    summary = Column(JSON, nullable=False)


class BacktestTrade(Base):
    __tablename__ = "backtest_trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, ForeignKey("backtest_runs.id"), nullable=False, index=True)
    side = Column(String, nullable=False)
    entry_ts = Column(DateTime, nullable=False)
    exit_ts = Column(DateTime, nullable=True)
    entry_px = Column(Float, nullable=False)
    exit_px = Column(Float, nullable=True)
    qty = Column(Float, nullable=False)
    gross_pnl = Column(Float, nullable=False, default=0.0)
    fees = Column(Float, nullable=False, default=0.0)
    net_pnl = Column(Float, nullable=False, default=0.0)
    return_pct = Column(Float, nullable=True)
    r_multiple = Column(Float, nullable=True)
    exit_reason = Column(String, nullable=True)
    payload = Column(JSON, nullable=False)


class BacktestEquityPoint(Base):
    __tablename__ = "backtest_equity_points"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, ForeignKey("backtest_runs.id"), nullable=False, index=True)
    ts = Column(DateTime, nullable=False, index=True)
    equity = Column(Float, nullable=False)
    cash = Column(Float, nullable=False)
    position_value = Column(Float, nullable=False, default=0.0)
    drawdown_pct = Column(Float, nullable=False, default=0.0)
