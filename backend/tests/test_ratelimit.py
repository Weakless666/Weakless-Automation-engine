"""Shared rate limits: every API instance enforces one combined limit for sign-in, codes and hooks."""
from __future__ import annotations

import threading

from sqlalchemy import func, select

from app import db as dbmod
from app.config import get_settings
from app.core import ratelimit
from app.core.ratelimit import DatabaseLimiter, SlidingWindowLimiter, purge_expired
from app.models import RateLimitCounter
from tests.conftest import make_user


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_two_instances_share_one_limit_where_memory_limiters_do_not(db):
    a, b = DatabaseLimiter(), DatabaseLimiter()  # two API processes, one database
    results = [x.allow("login-email:ada@example.com", 5) for x in (a, b, a, b, a, b, a)]
    assert results == [True] * 5 + [False, False]
    ma, mb = SlidingWindowLimiter(), SlidingWindowLimiter()  # what per-process limits would allow
    assert sum(x.allow("k", 5) for x in (ma, mb) * 5) == 10
    stored = db.scalars(select(RateLimitCounter.bucket)).all()
    assert stored and all(len(k) == 64 for k in stored) and not any("ada" in k for k in stored)  # only digests are stored


def test_sliding_window_weights_the_previous_window():
    clock = Clock(6000 * 60 + 10)  # 10 s into a minute window
    lim = DatabaseLimiter(clock)
    assert [lim.allow("k", 5) for _ in range(6)] == [True] * 5 + [False]
    clock.t += 60 + 20  # 30 s into the next window: half of the previous 5 still counts
    assert [lim.allow("k", 5) for _ in range(3)] == [True, True, False]
    clock.t += 60 * 5  # long idle: nothing counts any more
    assert all(lim.allow("k", 5) for _ in range(5))


def test_concurrent_requests_cannot_exceed_the_limit():
    lim, allowed, lock = DatabaseLimiter(), [], threading.Lock()

    def hit():
        ok = lim.allow("burst", 7)
        with lock:
            allowed.append(ok)

    threads = [threading.Thread(target=hit) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(allowed) == 7


def test_sign_in_limit_survives_an_instance_restart(client, db, monkeypatch):
    monkeypatch.setenv("WAE_AUTH_RATE_LIMIT_PER_MINUTE", "3")
    get_settings.cache_clear()
    make_user(db, "limited@example.com")
    attempt = lambda: client.post("/api/v1/auth/login", json={"email": "limited@example.com", "password": "wrong-password-1"}).status_code  # noqa: E731
    assert [attempt() for _ in range(3)] == [401, 401, 401]
    ratelimit.limiter.reset()  # what a restarted or different instance would have in memory
    assert attempt() == 429
    monkeypatch.setenv("WAE_RATE_LIMIT_STORE", "memory")  # single-instance mode keeps limits in process only
    get_settings.cache_clear()
    assert attempt() == 401


def test_database_problems_do_not_block_requests(monkeypatch, caplog):
    def broken():
        raise RuntimeError("database down")

    monkeypatch.setattr(dbmod, "get_engine", broken)
    assert DatabaseLimiter().allow("k", 1) and DatabaseLimiter().allow("k", 1)
    assert "shared rate limiter unavailable" in caplog.text


def test_finished_windows_are_purged(db):
    clock = Clock(1_000_000.0)
    DatabaseLimiter(clock).allow("old", 10)
    DatabaseLimiter().allow("current", 10)
    assert purge_expired(db) == 1
    assert db.scalar(select(func.count()).select_from(RateLimitCounter)) == 1
