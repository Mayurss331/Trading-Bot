from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, JSON, String, UniqueConstraint

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
