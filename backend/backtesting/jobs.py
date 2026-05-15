from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from backend.utils import clean


@dataclass
class BacktestJob:
    id: str
    status: str = "queued"
    stage: str = "queued"
    progress: float = 0.0
    message: str = "Queued"
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    config: dict[str, Any] = field(default_factory=dict)
    events: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None


_JOBS: dict[str, BacktestJob] = {}
_LOCK = threading.RLock()
_MAX_JOBS = 25


def _trim_jobs_locked() -> None:
    if len(_JOBS) <= _MAX_JOBS:
        return
    ordered = sorted(_JOBS.values(), key=lambda job: job.updated_at)
    for job in ordered[: max(0, len(_JOBS) - _MAX_JOBS)]:
        if job.status in {"completed", "failed"}:
            _JOBS.pop(job.id, None)


def create_job(config: dict[str, Any]) -> dict[str, Any]:
    job = BacktestJob(id=uuid.uuid4().hex[:12], config=config)
    with _LOCK:
        _JOBS[job.id] = job
        _trim_jobs_locked()
    return job_payload(job.id)


def update_job(
    job_id: str,
    *,
    status: str | None = None,
    stage: str | None = None,
    progress: float | None = None,
    message: str | None = None,
    event: str | None = None,
    stats: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return None
        if status is not None:
            job.status = status
        if stage is not None:
            job.stage = stage
        if progress is not None:
            job.progress = max(0.0, min(float(progress), 1.0))
        if message is not None:
            job.message = message
        if event:
            job.events.append(event)
            job.events = job.events[-120:]
        if stats:
            job.stats.update(clean(stats))
        job.updated_at = datetime.utcnow()
    return job_payload(job_id)


def complete_job(job_id: str, result: dict[str, Any]) -> dict[str, Any] | None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return None
        job.status = "completed" if result.get("ok") else "failed"
        job.stage = "completed" if result.get("ok") else "failed"
        job.progress = 1.0
        job.message = f"Run #{result.get('run_id')} completed." if result.get("ok") else str(result.get("message") or "Backtest failed.")
        job.result = clean(result)
        job.error = None if result.get("ok") else job.message
        job.updated_at = datetime.utcnow()
    return job_payload(job_id)


def fail_job(job_id: str, error: str) -> dict[str, Any] | None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return None
        job.status = "failed"
        job.stage = "failed"
        job.message = error
        job.error = error
        job.updated_at = datetime.utcnow()
    return job_payload(job_id)


def job_payload(job_id: str) -> dict[str, Any] | None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return None
        return clean({
            "ok": True,
            "job_id": job.id,
            "status": job.status,
            "stage": job.stage,
            "progress": job.progress,
            "message": job.message,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "config": job.config,
            "events": list(job.events),
            "stats": dict(job.stats),
            "result": job.result,
            "error": job.error,
        })
