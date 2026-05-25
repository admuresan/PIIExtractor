# Read genai/toolkits/architectural_instructions before making changes.

"""In-memory extraction jobs — append logs + polling (requirement matcher pattern)."""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

from services.log_line_normalize import normalize_log_line

_Status = Literal["queued", "running", "complete", "error"]

_MAX_JOBS = 96
_MAX_LINES_PER_JOB = 512


_lock = threading.Lock()
_jobs: "OrderedDict[str, ExtractionJob]" = OrderedDict()


@dataclass
class ExtractionJob:
    example_id: str
    status: _Status = "queued"
    lines: List[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)
    finished_at: Optional[float] = None
    result: Optional[Dict[str, Any]] = None
    error_detail: Optional[str] = None


def _trim_jobs() -> None:
    while len(_jobs) > _MAX_JOBS:
        _jobs.popitem(last=False)


def create_job(example_id: str) -> str:
    job_id = uuid.uuid4().hex
    with _lock:
        _trim_jobs()
        _jobs[job_id] = ExtractionJob(example_id=example_id, status="queued")
    return job_id


def append_line(job_id: str, raw: str) -> None:
    """Append normalized line if non-empty (thread-safe)."""
    normalized = normalize_log_line(raw)
    if not normalized:
        return
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job.lines.append(normalized)
        if len(job.lines) > _MAX_LINES_PER_JOB:
            drop = len(job.lines) - _MAX_LINES_PER_JOB
            job.lines = job.lines[drop:]


def mark_running(job_id: str) -> None:
    with _lock:
        j = _jobs.get(job_id)
        if j is not None:
            j.status = "running"


def complete_job(job_id: str, payload: Dict[str, Any]) -> None:
    with _lock:
        j = _jobs.get(job_id)
        if j is None:
            return
        j.status = "complete"
        j.result = payload
        j.finished_at = time.monotonic()


def fail_job(job_id: str, detail: str) -> None:
    with _lock:
        j = _jobs.get(job_id)
        if j is None:
            return
        j.status = "error"
        j.error_detail = detail
        j.finished_at = time.monotonic()


def snapshot_progress(job_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return None

        elapsed = (
            job.finished_at if job.finished_at is not None else time.monotonic()
        ) - job.started_at
        elapsed = max(0.0, elapsed)

        last_line = job.lines[-1] if job.lines else None

        out: Dict[str, Any] = {
            "ok": True,
            "job_id": job_id,
            "example_id": job.example_id,
            "status": job.status,
            "elapsed_seconds": round(elapsed, 2),
            "lines": list(job.lines),
            "last_line": last_line,
        }
        if job.status == "complete" and job.result is not None:
            out["result"] = job.result
        if job.status == "error":
            out["error"] = "extraction_failed"
            out["detail"] = job.error_detail or "Unknown error"

        return out
