#!/usr/bin/env python3
"""
CoinDCX Bot Dashboard — FastAPI backend.

Run with: uvicorn backend.main:app --reload --port 8000
"""
from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ROOT / "frontend"

# Ensure project root is on path (for strategies/ package)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env(ROOT / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

# Deferred imports (after env loaded and path set)
from backend.db.database import init_db  # noqa: E402
from backend.tasks.candle_store import aggregate_candles  # noqa: E402
from backend.routers.snapshot import router as snapshot_router  # noqa: E402
from backend.routers.account import router as account_router  # noqa: E402
from backend.routers.ws import router as ws_router  # noqa: E402
from backend.routers.intelligence import router as intelligence_router  # noqa: E402

scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up — initialising database …")
    await init_db()
    logger.info("Database ready.")

    scheduler.add_job(aggregate_candles, "interval", minutes=5, id="candle_store",
                      max_instances=1, coalesce=True)
    scheduler.start()
    logger.info("APScheduler started (candle aggregation every 5 min).")

    yield

    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped. Goodbye.")


app = FastAPI(
    title="CoinDCX Bot Dashboard",
    description="FastAPI backend for the confluence trading bot.",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(snapshot_router)
app.include_router(account_router)
app.include_router(ws_router)
app.include_router(intelligence_router)

# Serve the frontend last (catch-all)
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
