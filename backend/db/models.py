from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Integer, JSON, String, UniqueConstraint

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
