from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

ROOT = Path(__file__).resolve().parents[2]
DATABASE_URL = f"sqlite+aiosqlite:///{ROOT}/tradingbot.db"

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


async def init_db() -> None:
    from .models import (  # noqa: F401
        AccountSnapshot,
        Candle,
        PositionSnapshot,
        SignalEvent,
        StateSnapshot,
        Trade,
        UserSetting,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
