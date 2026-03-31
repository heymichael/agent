"""Step-level execution tracking for vendor sync jobs.

Writes to sync_job_log (one row per run) and sync_job_step (one row per step).
Designed to be used across all sync scripts — AWS, GCP, Bill.com, etc.

Usage:
    tracker = SyncTracker("aws-spend-sync", pool)
    tracker.start()
    with tracker.step("api_fetch") as s:
        rows = call_api()
        s.row_count = len(rows)
    with tracker.step("detail_upsert") as s:
        upsert(rows)
        s.row_count = len(rows)
    tracker.finish()
"""

import json
import logging
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ms_since(t: float) -> int:
    return int((time.time() - t) * 1000)


class StepContext:
    """Mutable context passed into a step block so callers can set row_count / metadata."""

    def __init__(self):
        self.row_count: int | None = None
        self.metadata: dict[str, Any] = {}


_INSERT_JOB = """
    INSERT INTO sync_job_log (job_name, status, started_at, metadata)
    VALUES (%s, 'running', %s, %s)
    RETURNING id
"""

_FINISH_JOB = """
    UPDATE sync_job_log
    SET status = %s, finished_at = %s, duration_ms = %s, error = %s
    WHERE id = %s
"""

_INSERT_STEP = """
    INSERT INTO sync_job_step (job_id, step_name, status, started_at)
    VALUES (%s, %s, 'running', %s)
    RETURNING id
"""

_FINISH_STEP = """
    UPDATE sync_job_step
    SET status = %s, finished_at = %s, duration_ms = %s,
        row_count = %s, error = %s, metadata = %s
    WHERE id = %s
"""


class SyncTracker:
    """Tracks a single sync job run with step-level granularity."""

    def __init__(self, job_name: str, pool):
        self.job_name = job_name
        self._pool = pool
        self._job_id: str | None = None
        self._start_time: float | None = None
        self._failed = False

    def start(self, metadata: dict | None = None):
        now = _now()
        self._start_time = time.time()
        with self._pool.connection() as conn:
            row = conn.execute(
                _INSERT_JOB,
                (self.job_name, now, json.dumps(metadata or {})),
            ).fetchone()
            self._job_id = str(row["id"])
        logger.info("[%s] Job started (run %s)", self.job_name, self._job_id)

    @contextmanager
    def step(self, step_name: str):
        """Context manager for a single step. Yields a StepContext for setting row_count/metadata."""
        now = _now()
        step_start = time.time()
        ctx = StepContext()

        with self._pool.connection() as conn:
            row = conn.execute(_INSERT_STEP, (self._job_id, step_name, now)).fetchone()
            step_id = str(row["id"])

        logger.info("[%s] Step '%s' started", self.job_name, step_name)

        try:
            yield ctx
        except Exception as exc:
            self._failed = True
            err_text = traceback.format_exc()
            with self._pool.connection() as conn:
                conn.execute(_FINISH_STEP, (
                    "failed", _now(), _ms_since(step_start),
                    ctx.row_count, err_text, json.dumps(ctx.metadata),
                    step_id,
                ))
            logger.error("[%s] Step '%s' failed: %s", self.job_name, step_name, exc)
            raise
        else:
            with self._pool.connection() as conn:
                conn.execute(_FINISH_STEP, (
                    "completed", _now(), _ms_since(step_start),
                    ctx.row_count, None, json.dumps(ctx.metadata),
                    step_id,
                ))
            logger.info(
                "[%s] Step '%s' completed (rows=%s, %dms)",
                self.job_name, step_name, ctx.row_count, _ms_since(step_start),
            )

    def finish(self, error: str | None = None):
        status = "failed" if (error or self._failed) else "completed"
        with self._pool.connection() as conn:
            conn.execute(_FINISH_JOB, (
                status, _now(), _ms_since(self._start_time),
                error, self._job_id,
            ))
        logger.info(
            "[%s] Job %s (%dms)",
            self.job_name, status, _ms_since(self._start_time),
        )

    def fail(self, error: str):
        """Mark the overall job as failed (call instead of finish when catching top-level errors)."""
        self._failed = True
        self.finish(error=error)
