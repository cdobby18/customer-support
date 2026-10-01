"""Rate limiting with an optional shared backend.

The login, registration, inbound-webhook and LLM throttles used to each keep
their own process-local dict. Process-local state under-counts by the number of
workers (N replicas = N x the configured limit), and the webhook fallback never
evicted idle clients, so it grew without bound. This module is the single place
that decides where a counter lives so every throttle shares one backend.

Memory is the default and the fallback: a development machine, the test suite
and a single-worker deployment need no Redis. Set ``RATE_LIMIT_BACKEND=redis``
to put the windows in Redis so the limit becomes cluster-wide; a Redis that
cannot be reached degrades to memory rather than refusing every request, which
is preferable to a throttle that takes the service down with it.
"""

import os
import threading
import time
import uuid

DEFAULT_WINDOW_SECONDS = 300

_memory: dict[str, list[float]] = {}
_lock = threading.Lock()

_redis_client = None
_redis_resolved = False


def rate_limit_backend() -> str:
    return os.getenv("RATE_LIMIT_BACKEND", "memory").strip().lower()


def _redis():
    """Lazily connect once. Returns None for the memory backend or on failure."""
    global _redis_client, _redis_resolved
    if _redis_resolved:
        return _redis_client
    _redis_resolved = True
    if rate_limit_backend() != "redis":
        return None
    try:
        import redis

        client = redis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
        )
        client.ping()
        _redis_client = client
    except Exception:
        _redis_client = None
    return _redis_client


def reset_rate_limits() -> None:
    """Forget every window. Used by tests and after a backend reconfiguration."""
    global _redis_client, _redis_resolved
    with _lock:
        _memory.clear()
    _redis_resolved = False
    _redis_client = None


def check(key: str, limit: int, window_seconds: int) -> int:
    """Seconds to wait when `key` has spent its budget, else 0. Does not record."""
    if limit <= 0:
        return 0
    client = _redis()
    if client is not None:
        return _redis_check(client, key, limit, window_seconds)
    now = time.monotonic()
    with _lock:
        bucket = _prune(key, now, window_seconds)
        if len(bucket) >= limit:
            return _retry_after(bucket[0], now, window_seconds)
    return 0


def record(key: str, limit: int, window_seconds: int) -> int:
    """Add one event to `key`'s window. Returns the count afterwards."""
    if limit <= 0:
        return 0
    client = _redis()
    if client is not None:
        return _redis_record(client, key, window_seconds)
    now = time.monotonic()
    with _lock:
        bucket = _prune(key, now, window_seconds)
        bucket.append(now)
        bucket = bucket[-limit:]
        _memory[key] = bucket
        return len(bucket)


def count(key: str, window_seconds: int) -> int:
    """How many events are currently in `key`'s window."""
    client = _redis()
    if client is not None:
        try:
            return int(client.zcount(key, f"({time.time() - window_seconds}", "+inf"))
        except Exception:
            return 0
    with _lock:
        return len(_prune(key, time.monotonic(), window_seconds))


def clear(key: str) -> None:
    client = _redis()
    if client is not None:
        try:
            client.delete(key)
        except Exception:
            pass
    with _lock:
        _memory.pop(key, None)


def _prune(key: str, now: float, window_seconds: int) -> list[float]:
    """Drop expired events from `key` and return what is left. Caller holds _lock."""
    bucket = [ts for ts in _memory.get(key, []) if ts > now - window_seconds]
    if bucket:
        _memory[key] = bucket
    else:
        _memory.pop(key, None)
    return bucket


def _retry_after(oldest: float, now: float, window_seconds: int) -> int:
    return max(1, int(oldest + window_seconds - now) + 1)


def _redis_check(client, key: str, limit: int, window_seconds: int) -> int:
    now = time.time()
    try:
        client.zremrangebyscore(key, "-inf", now - window_seconds)
        if int(client.zcard(key)) < limit:
            return 0
        oldest = client.zrange(key, 0, 0, withscores=True)
        if oldest:
            return _retry_after(float(oldest[0][1]), now, window_seconds)
        return max(1, window_seconds)
    except Exception:
        return 0


def _redis_record(client, key: str, window_seconds: int) -> int:
    now = time.time()
    member = f"{now}:{uuid.uuid4().hex}"
    try:
        pipe = client.pipeline()
        pipe.zadd(key, {member: now})
        pipe.zremrangebyscore(key, "-inf", now - window_seconds)
        pipe.zcard(key)
        pipe.expire(key, window_seconds + 1)
        return int(pipe.execute()[2])
    except Exception:
        return 0
