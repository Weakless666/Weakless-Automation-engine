"""Durable job queue and worker.

Jobs live in the ``execution_queues`` table, so queued, delayed and waiting
work survives restarts. PostgreSQL workers claim rows with
``FOR UPDATE SKIP LOCKED`` (safe with any number of workers); SQLite uses a
compare-and-swap update (single-node development).

Reliability:
* visibility timeout - a job claimed by a worker that died is re-queued with
  ``recover=True`` so the executor can apply its crash rules;
* infrastructure failures are retried with exponential back-off + jitter;
* after ``max_attempts`` the job moves to ``DEAD`` (dead-letter queue) and the
  execution is failed; operators can inspect and requeue dead letters;
* per-organization concurrency limits defer jobs instead of running them.
"""
from __future__ import annotations

import logging
import socket
import threading
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.config import get_settings
from app.db import new_session, utcnow
from app.engine.executor import backoff_seconds, run_execution
from app.models import ExecutionQueueJob, TERMINAL_STATUSES, WorkflowExecution

log = logging.getLogger("wae.queue")
MAX_BUSY_DEFERRALS = 300
RETENTION_INTERVAL_SECONDS = 3600


def enqueue(
    db: Session,
    organization_id: uuid.UUID,
    execution_id: uuid.UUID | None,
    *,
    run_at: datetime | None = None,
    payload: dict | None = None,
    kind: str = "execution",
) -> ExecutionQueueJob:
    job = ExecutionQueueJob(
        organization_id=organization_id,
        execution_id=execution_id,
        kind=kind,
        payload=payload or {},
        run_at=run_at or utcnow(),
        max_attempts=get_settings().job_max_attempts,
    )
    db.add(job)
    db.flush()
    return job


def claim(db: Session, worker_id: str) -> ExecutionQueueJob | None:
    now = utcnow()
    base = (
        select(ExecutionQueueJob)
        .where(ExecutionQueueJob.status == "QUEUED", ExecutionQueueJob.run_at <= now)
        .order_by(ExecutionQueueJob.run_at)
        .limit(1)
    )
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        job = db.scalar(base.with_for_update(skip_locked=True))
        if not job:
            db.rollback()
            return None
        job.status, job.locked_by, job.locked_at = "CLAIMED", worker_id, now
        job.attempts += 1
        db.commit()
        return job
    # Without row locks the claim is a compare-and-swap. Losing it means another worker took that
    # job, not that the queue is empty, so look again instead of reporting an idle queue.
    for _ in range(50):
        job = db.scalar(base)
        if not job:
            db.rollback()
            return None
        res = db.execute(
            update(ExecutionQueueJob)
            .where(ExecutionQueueJob.id == job.id, ExecutionQueueJob.status == "QUEUED")
            .values(status="CLAIMED", locked_by=worker_id, locked_at=now, attempts=ExecutionQueueJob.attempts + 1)
            .execution_options(synchronize_session=False)
        )
        db.commit()
        if res.rowcount:
            db.refresh(job)
            return job
    return None


def _dead_letter(db: Session, job: ExecutionQueueJob, reason: str) -> None:
    job.status, job.last_error, job.finished_at = "DEAD", reason[:2000], utcnow()
    if job.execution_id:
        ex = db.get(WorkflowExecution, job.execution_id)
        if ex and ex.status not in TERMINAL_STATUSES:
            ex.status, ex.finished_at = "FAILED", utcnow()
            ex.error = {"code": "dead_letter", "message": "Execution could not be processed and was moved to the dead-letter queue"}
            try:
                from app.services.events import on_execution_finished

                on_execution_finished(db, ex)
            except Exception:  # noqa: BLE001
                log.exception("dead-letter hooks failed")


def reclaim_stale(db: Session) -> int:
    """Re-queue jobs whose worker stopped heartbeating (visibility timeout)."""
    cutoff = utcnow() - timedelta(seconds=get_settings().job_visibility_timeout_seconds)
    stale = db.scalars(
        select(ExecutionQueueJob).where(ExecutionQueueJob.status == "CLAIMED", ExecutionQueueJob.locked_at < cutoff)
    ).all()
    for job in stale:
        if job.attempts >= job.max_attempts:
            _dead_letter(db, job, "Worker stopped repeatedly while processing this job")
        else:
            job.status, job.locked_by, job.locked_at = "QUEUED", None, None
            job.payload = {**(job.payload or {}), "recover": True}
            flag_modified(job, "payload")
            job.run_at = utcnow()
    db.commit()
    return len(stale)


def _org_concurrency_limit(db: Session, organization_id: uuid.UUID) -> int:
    from app.services.usage import plan_limit

    limit = plan_limit(db, organization_id, "concurrency")
    return int(limit) if limit else get_settings().default_org_concurrency


def process_job(job_id: uuid.UUID, worker_id: str = "inline") -> str:
    """Process one claimed job in its own tenant-scoped session."""
    with new_session(bypass_rls=True) as admin:
        job = admin.get(ExecutionQueueJob, job_id)
        if not job:
            return "missing"
        org_id, execution_id, payload, kind = job.organization_id, job.execution_id, dict(job.payload or {}), job.kind
        is_start = not (payload.get("resume_node") or payload.get("timeout_node") or payload.get("recover"))
        if execution_id and is_start:
            running = admin.scalar(
                select(func.count()).select_from(WorkflowExecution).where(
                    WorkflowExecution.organization_id == org_id, WorkflowExecution.status == "RUNNING"
                )
            )
            if running >= _org_concurrency_limit(admin, org_id):
                job.status, job.locked_by, job.locked_at = "QUEUED", None, None
                job.attempts = max(0, job.attempts - 1)
                job.run_at = utcnow() + timedelta(seconds=2)
                admin.commit()
                return "deferred"

    outcome, error = "", None
    try:
        with new_session(org_id=org_id) as db:
            if kind == "agent_resume":
                from app.ai.agents import resume_after_approval

                outcome = resume_after_approval(db, payload)
            else:
                outcome = run_execution(db, execution_id, payload) if execution_id else "missing"
    except Exception as exc:  # noqa: BLE001 - infrastructure failure (database, bug in the engine)
        log.exception("Job %s crashed", job_id)
        error = f"{type(exc).__name__}: {exc}"

    with new_session(bypass_rls=True) as admin:
        job = admin.get(ExecutionQueueJob, job_id)
        if not job:
            return outcome
        if error is not None:
            if job.attempts >= job.max_attempts:
                _dead_letter(admin, job, error)
                outcome = "dead"
            else:
                job.status, job.locked_by, job.locked_at, job.last_error = "QUEUED", None, None, error[:2000]
                job.payload = {**(job.payload or {}), "recover": True}
                flag_modified(job, "payload")
                job.run_at = utcnow() + timedelta(seconds=backoff_seconds(job.attempts))
                outcome = "requeued"
        elif outcome == "busy":
            busy = int((job.payload or {}).get("_busy", 0)) + 1
            if busy > MAX_BUSY_DEFERRALS:
                _dead_letter(admin, job, "Execution stayed locked by another worker")
                outcome = "dead"
            else:
                job.status, job.locked_by, job.locked_at = "QUEUED", None, None
                job.attempts = max(0, job.attempts - 1)
                job.payload = {**(job.payload or {}), "_busy": busy}
                flag_modified(job, "payload")
                job.run_at = utcnow() + timedelta(seconds=2)
        else:
            job.status, job.finished_at = "DONE", utcnow()
        admin.commit()
    return outcome


class Worker:
    def __init__(self, worker_id: str | None = None):
        self.worker_id = worker_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self._stop = threading.Event()
        self._last_maintenance = 0.0
        self._last_retention = float("-inf")  # first sweep right after start-up

    def run_once(self) -> str | None:
        """Claim and process a single job. Returns the outcome or None if the queue is idle."""
        with new_session(bypass_rls=True) as db:
            job = claim(db, self.worker_id)
            job_id = job.id if job else None
        if job_id is None:
            return None
        return process_job(job_id, self.worker_id)

    def maintenance(self) -> None:
        from app.services.approvals import expire_approvals
        from app.services.scheduler import run_due_schedules

        with new_session(bypass_rls=True) as db:
            reclaim_stale(db)
        with new_session(bypass_rls=True) as db:
            run_due_schedules(db)
        with new_session(bypass_rls=True) as db:
            from app.services.mailbox import poll_due_mailboxes

            poll_due_mailboxes(db)
        with new_session(bypass_rls=True) as db:
            from app.services.content import release_due_content

            release_due_content(db)
        with new_session(bypass_rls=True) as db:
            from app.services.studio import poll_generations

            poll_generations(db)
        with new_session(bypass_rls=True) as db:
            expire_approvals(db)
        with new_session(bypass_rls=True) as db:
            from app.services.billing import roll_periods

            roll_periods(db)

    def drain(self, max_jobs: int = 1000) -> int:
        """Process every job that is ready now (used by tests and the CLI)."""
        done = 0
        while done < max_jobs and self.run_once() is not None:
            done += 1
        return done

    def run_forever(self) -> None:
        import time

        s = get_settings()
        log.info("worker %s started", self.worker_id)
        while not self._stop.is_set():
            try:
                now = time.monotonic()
                if now - self._last_maintenance > 5:
                    self._last_maintenance = now
                    self.maintenance()
                if now - self._last_retention > RETENTION_INTERVAL_SECONDS:
                    self._last_retention = now
                    from app.services.retention import run_retention

                    with new_session(bypass_rls=True) as db:
                        run_retention(db)  # batched and idempotent, so concurrent workers are safe
                if self.run_once() is None:
                    self._stop.wait(s.worker_poll_seconds)
            except Exception:  # noqa: BLE001 - keep the worker alive
                log.exception("worker loop error")
                self._stop.wait(2)
        log.info("worker %s stopped", self.worker_id)

    def stop(self) -> None:
        self._stop.set()


def queue_stats(db: Session) -> dict:
    now = utcnow()
    rows = dict(db.execute(select(ExecutionQueueJob.status, func.count()).group_by(ExecutionQueueJob.status)).all())
    oldest = db.scalar(
        select(func.min(ExecutionQueueJob.run_at)).where(ExecutionQueueJob.status == "QUEUED", ExecutionQueueJob.run_at <= now)
    )
    delayed = db.scalar(
        select(func.count()).select_from(ExecutionQueueJob).where(ExecutionQueueJob.status == "QUEUED", ExecutionQueueJob.run_at > now)
    )
    ready = max(0, int(rows.get("QUEUED", 0)) - int(delayed or 0))
    return {
        "ready": ready,
        "scheduled": int(delayed or 0),
        "claimed": int(rows.get("CLAIMED", 0)),
        "dead": int(rows.get("DEAD", 0)),
        "oldest_ready_age_seconds": round((now - oldest).total_seconds(), 1) if oldest else 0,
    }
