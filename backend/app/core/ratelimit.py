"""Rate limiters.

``limiter`` is an in-process sliding window. It holds the general per-client API budget, which is
checked on every request and does not need to be exact across instances (with N API instances a
client can reach N times the budget).

``shared_limiter`` holds every limit that protects against guessing or abuse: sign-in, password
reset, two-factor codes, invitation links, inbound hooks and resume links. With
``WAE_RATE_LIMIT_STORE=database`` (the default) its state lives in the ``rate_limit_counters``
table, so all API instances and workers enforce one combined limit:

* sliding-window counter: two fixed windows, the previous one weighted by how much of it still
  overlaps the sliding window (the approach used by large API gateways; at most a few percent
  above the exact count at window boundaries);
* each hit is one atomic ``INSERT ... ON CONFLICT DO UPDATE ... RETURNING`` in its own short
  transaction, independent of the request's transaction;
* keys are stored as SHA-256 digests, so e-mail addresses and IP addresses are not written to the
  database;
* if the database cannot be reached the limiter allows the request and logs a warning, so a
  database outage is reported by the request itself rather than as a rate-limit error.

``WAE_RATE_LIMIT_STORE=memory`` keeps everything in process (single-instance deployments).
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import defaultdict, deque

log = logging.getLogger("wae.ratelimit")


class SlidingWindowLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_seconds: float = 60.0) -> bool:
        if limit <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - window_seconds:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            if len(self._hits) > 50000:  # bound memory
                for k in [k for k, v in self._hits.items() if not v]:
                    del self._hits[k]
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


class DatabaseLimiter:
    """Sliding-window counter shared by every process that uses the same database."""

    def __init__(self, clock=time.time) -> None:
        self._clock = clock

    @staticmethod
    def digest(key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest()

    def allow(self, key: str, limit: int, window_seconds: float = 60.0) -> bool:
        if limit <= 0:
            return True
        from sqlalchemy import select

        from app import db as dbmod
        from app.models import RateLimitCounter as C

        get_engine = dbmod.get_engine

        window = max(1, int(window_seconds))
        now = self._clock()
        current = int(now // window) * window
        bucket = self.digest(key)
        try:
            engine = get_engine()
            if engine.dialect.name == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
            else:
                from sqlalchemy.dialects.sqlite import insert
            with engine.begin() as conn:
                previous = conn.scalar(select(C.hits).where(C.bucket == bucket, C.window_start == current - window)) or 0
                weight = 1 - (now - current) / window
                estimate = previous * weight
                if estimate + self._peek(conn, C, bucket, current) >= limit:
                    return False  # rejected attempts are not counted: a blocked client cannot extend its own block
                stmt = insert(C).values(bucket=bucket, window_start=current, hits=1, expires_at=current + 2 * window)
                hits = conn.scalar(stmt.on_conflict_do_update(index_elements=[C.bucket, C.window_start],
                                                              set_={"hits": C.hits + 1}).returning(C.hits))
                return estimate + (hits or 0) <= limit
        except Exception:  # noqa: BLE001 - never turn a database problem into a rate-limit error
            log.warning("shared rate limiter unavailable; allowing request", exc_info=True)
            return True

    @staticmethod
    def _peek(conn, C, bucket: str, window_start: int) -> int:
        from sqlalchemy import select

        return conn.scalar(select(C.hits).where(C.bucket == bucket, C.window_start == window_start)) or 0

    def reset(self) -> None:
        """Tests: every test database starts empty, so there is nothing to clear here."""


class _Configured:
    """Chooses the backend from settings on each call, so tests and CLI tools can switch it."""

    def __init__(self) -> None:
        self._db = DatabaseLimiter()

    def _backend(self):
        from app.config import get_settings

        return self._db if get_settings().rate_limit_store == "database" else limiter

    def allow(self, key: str, limit: int, window_seconds: float = 60.0) -> bool:
        return self._backend().allow(key, limit, window_seconds)

    def reset(self) -> None:
        limiter.reset()


limiter = SlidingWindowLimiter()
shared_limiter = _Configured()


def purge_expired(db, now: float | None = None) -> int:
    """Remove finished windows (run by the retention sweep)."""
    from sqlalchemy import delete

    from app.models import RateLimitCounter

    res = db.execute(delete(RateLimitCounter).where(RateLimitCounter.expires_at < int(now or time.time())))
    db.commit()
    return res.rowcount or 0
