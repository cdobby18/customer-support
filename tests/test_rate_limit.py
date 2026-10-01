"""The rate-limit backend is one decision, shared by every throttle.

The bug this pins: login, registration, inbound webhooks and the LLM gateway
each kept their own process-local counter, so N replicas each allowed the full
limit. `app.security.rate_limit` is now the single place that decides where a
window lives, and `RATE_LIMIT_BACKEND=redis` puts it where all workers see it.
"""

from __future__ import annotations

import pytest

from app.security import rate_limit


@pytest.fixture(autouse=True)
def _clean_windows():
    rate_limit.reset_rate_limits()
    yield
    rate_limit.reset_rate_limits()


def _memory_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "memory")
    monkeypatch.setattr(rate_limit, "_redis", lambda: None)


def test_backend_defaults_to_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RATE_LIMIT_BACKEND", raising=False)
    assert rate_limit.rate_limit_backend() == "memory"


def test_check_is_zero_until_the_budget_is_spent(monkeypatch: pytest.MonkeyPatch) -> None:
    _memory_only(monkeypatch)

    assert rate_limit.check("k", 3, 60) == 0
    assert rate_limit.record("k", 3, 60) == 1
    assert rate_limit.check("k", 3, 60) == 0
    assert rate_limit.record("k", 3, 60) == 2
    assert rate_limit.check("k", 3, 60) == 0


def test_check_reports_a_wait_once_the_budget_is_spent(monkeypatch: pytest.MonkeyPatch) -> None:
    _memory_only(monkeypatch)
    for _ in range(3):
        rate_limit.record("k", 3, 60)

    retry_after = rate_limit.check("k", 3, 60)

    assert 0 < retry_after <= 60


def test_a_limit_of_zero_disables_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    _memory_only(monkeypatch)
    for _ in range(5):
        rate_limit.record("k", 0, 60)

    assert rate_limit.check("k", 0, 60) == 0
    assert rate_limit.count("k", 60) == 0


def test_windows_are_per_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _memory_only(monkeypatch)
    for _ in range(3):
        rate_limit.record("a", 3, 60)

    assert rate_limit.check("a", 3, 60) > 0
    assert rate_limit.check("b", 3, 60) == 0


def test_the_window_never_exceeds_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unbounded list is the memory leak the shared backend also fixes."""
    _memory_only(monkeypatch)
    for _ in range(50):
        rate_limit.record("k", 5, 60)

    assert rate_limit.count("k", 60) == 5
    assert len(rate_limit._memory["k"]) == 5


def test_expired_events_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    _memory_only(monkeypatch)
    monkeypatch.setenv("AUTH_LOGIN_LOCKOUT_SECONDS", "1")
    for _ in range(3):
        rate_limit.record("k", 3, 1)
    monkeypatch.setattr(
        rate_limit.time, "monotonic", lambda: rate_limit.time.perf_counter() + 5
    )

    assert rate_limit.check("k", 3, 1) == 0
    assert rate_limit.count("k", 1) == 0
    assert "k" not in rate_limit._memory


def test_clear_empties_one_window_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _memory_only(monkeypatch)
    rate_limit.record("a", 3, 60)
    rate_limit.record("b", 3, 60)

    rate_limit.clear("a")

    assert rate_limit.count("a", 60) == 0
    assert rate_limit.count("b", 60) == 1


def test_reset_forgets_every_window(monkeypatch: pytest.MonkeyPatch) -> None:
    _memory_only(monkeypatch)
    rate_limit.record("a", 3, 60)
    rate_limit.record("b", 3, 60)

    rate_limit.reset_rate_limits()

    assert rate_limit._memory == {}


class FakeRedis:
    """Enough of the redis API for the sorted-set window to be exercised."""

    def __init__(self) -> None:
        self.zsets: dict[str, dict[str, float]] = {}
        self.ttls: dict[str, int] = {}
        self.calls: list[str] = []

    def ping(self) -> bool:
        self.calls.append("ping")
        return True

    def delete(self, key: str) -> None:
        self.calls.append(f"delete {key}")
        self.zsets.pop(key, None)

    def zadd(self, key: str, mapping: dict[str, float]) -> None:
        self.calls.append(f"zadd {key}")
        self.zsets.setdefault(key, {}).update(mapping)

    def zremrangebyscore(self, key: str, low: str, high: float) -> None:
        self.calls.append(f"zremrangebyscore {key}")
        bucket = self.zsets.get(key)
        if not bucket:
            return
        cutoff = float(low) if low != "-inf" else float("-inf")
        for member in [m for m, score in bucket.items() if score <= high and score > cutoff]:
            del bucket[member]

    def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))

    def zcount(self, key: str, low: str, high: str) -> int:
        cutoff = float(low.strip("("))
        return len([1 for score in self.zsets.get(key, {}).values() if score > cutoff])

    def zrange(self, key: str, start: int, end: int, withscores: bool = False):
        self.calls.append(f"zrange {key}")
        ordered = sorted(self.zsets.get(key, {}).items(), key=lambda item: item[1])
        window = ordered[start : end + 1]
        return window if withscores else [member for member, _ in window]

    def expire(self, key: str, seconds: int) -> None:
        self.ttls[key] = seconds

    def pipeline(self):
        calls: list = []

        class Pipeline:
            def __init__(self, redis_client: FakeRedis) -> None:
                self._redis = redis_client

            def zadd(self, key, mapping):
                calls.append(("zadd", key, mapping))

            def zremrangebyscore(self, key, low, high):
                calls.append(("zremrangebyscore", key, low, high))

            def zcard(self, key):
                calls.append(("zcard", key))

            def expire(self, key, seconds):
                calls.append(("expire", key, seconds))

            def execute(self):
                results = []
                for call in calls:
                    results.append(getattr(self._redis, call[0])(*call[1:]))
                calls.clear()
                return results

        return Pipeline(self)


def _with_redis(monkeypatch: pytest.MonkeyPatch, fake: FakeRedis) -> None:
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "redis")
    monkeypatch.setattr(rate_limit, "_redis", lambda: fake)


def test_redis_backend_is_used_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRedis()
    _with_redis(monkeypatch, fake)

    rate_limit.record("k", 3, 60)

    assert any(call.startswith("zadd k") for call in fake.calls)
    assert rate_limit.count("k", 60) == 1


def test_redis_backend_enforces_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of the shared backend: two workers share one budget."""
    fake = FakeRedis()
    _with_redis(monkeypatch, fake)
    for _ in range(3):
        rate_limit.record("k", 3, 60)

    retry_after = rate_limit.check("k", 3, 60)

    assert retry_after > 0


def test_redis_backend_sets_an_expiry_so_keys_do_not_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeRedis()
    _with_redis(monkeypatch, fake)

    rate_limit.record("k", 3, 60)

    assert fake.ttls["k"] > 0


def test_clear_reaches_redis_too(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRedis()
    _with_redis(monkeypatch, fake)
    rate_limit.record("k", 3, 60)

    rate_limit.clear("k")

    assert "delete k" in fake.calls


def test_an_unreachable_redis_degrades_to_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """A throttle that takes the service down is worse than a per-worker one."""
    import sys

    class BrokenRedisModule:
        @staticmethod
        def from_url(url, decode_responses=True):
            raise ConnectionError("redis is down")

    monkeypatch.setenv("RATE_LIMIT_BACKEND", "redis")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setitem(sys.modules, "redis", BrokenRedisModule)
    rate_limit.reset_rate_limits()

    # Resolution must not raise, and the window must still work in memory.
    assert rate_limit._redis() is None
    rate_limit.record("k", 3, 60)

    assert rate_limit.count("k", 60) == 1
    for _ in range(2):
        rate_limit.record("k", 3, 60)
    assert rate_limit.check("k", 3, 60) > 0


def test_redis_operational_errors_degrade_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Exploding(FakeRedis):
        def zcard(self, key: str) -> int:
            raise TimeoutError("redis timed out")

        def zcount(self, key: str, low: str, high: str) -> int:
            raise TimeoutError("redis timed out")

    _with_redis(monkeypatch, Exploding())

    assert rate_limit.check("k", 3, 60) == 0
    assert rate_limit.count("k", 60) == 0
    rate_limit.record("k", 3, 60)
