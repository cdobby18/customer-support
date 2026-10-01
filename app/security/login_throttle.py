"""Login brute-force throttling.

Failed logins are counted per (email, client host) in a process-local sliding
window. Once the count reaches the limit the pair is locked out for
AUTH_LOGIN_LOCKOUT_SECONDS, and further attempts are refused with 429 without
touching the password hash. A successful login clears the counter.

Scope: process-local, like the webhook and LLM rate limiters. It throttles one
worker; a multi-worker deployment needs a shared store (Redis) to enforce the
limit cluster-wide, otherwise the effective limit is `limit x workers`.

The client host is taken from the socket, not `X-Forwarded-For`, which is
untrusted input. Behind a reverse proxy every caller therefore shares the
proxy's address, so the window becomes per-email across all of them - still
throttles credential stuffing, but a rotating-IP attacker gets `limit x ips`
attempts per window. A trusted-proxy deployment should key on the forwarded
address instead.
"""

import os
import threading
import time

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_LOCKOUT_SECONDS = 300
DEFAULT_REGISTER_MAX_ATTEMPTS = 20

_attempt_windows: dict[str, list[float]] = {}
_window_lock = threading.Lock()


class LoginThrottled(Exception):
    """Raised when too many recent failed logins were recorded for a key."""

    def __init__(self, retry_after: int, attempts: int) -> None:
        super().__init__(
            f"Too many failed login attempts ({attempts}); retry in {retry_after}s"
        )
        self.retry_after = retry_after
        self.attempts = attempts


def max_login_attempts() -> int:
    try:
        return int(os.getenv("AUTH_LOGIN_MAX_ATTEMPTS", str(DEFAULT_MAX_ATTEMPTS)))
    except ValueError:
        return DEFAULT_MAX_ATTEMPTS


def login_lockout_seconds() -> int:
    try:
        return int(os.getenv("AUTH_LOGIN_LOCKOUT_SECONDS", str(DEFAULT_LOCKOUT_SECONDS)))
    except ValueError:
        return DEFAULT_LOCKOUT_SECONDS


def login_throttle_key(email: str, client_host: str) -> str:
    return f"{email.strip().lower()}|{client_host}"


def max_registration_attempts() -> int:
    try:
        return int(os.getenv("AUTH_REGISTER_MAX_ATTEMPTS", str(DEFAULT_REGISTER_MAX_ATTEMPTS)))
    except ValueError:
        return DEFAULT_REGISTER_MAX_ATTEMPTS


def registration_throttle_key(client_host: str) -> str:
    # Prefixed so it cannot collide with a login key, which is `email|host`.
    return f"register|{client_host}"


def check_registration_allowed(key: str) -> None:
    """Raise LoginThrottled when `key` has spent its registration budget.

    Registration is an unauthenticated write path, so without this an attacker
    can create accounts (and probe which emails already exist, since a duplicate
    answers 409) as fast as the server accepts requests.
    """
    limit = max_registration_attempts()
    if limit <= 0:
        return
    now = time.monotonic()
    with _window_lock:
        attempts = _pruned_attempts(key, now)
        retry_after = _refresh_lockout(key, attempts, now)
    if retry_after and len(attempts) >= limit:
        raise LoginThrottled(retry_after, len(attempts))


def record_registration_attempt(key: str) -> int:
    """Add one attempt to `key`'s window. Returns the count afterwards."""
    limit = max_registration_attempts()
    if limit <= 0:
        return 0
    with _window_lock:
        attempts = _pruned_attempts(key, time.monotonic())
        attempts.append(time.monotonic())
        attempts = attempts[-limit:]
        _attempt_windows[key] = attempts
    return len(attempts)


def retry_after_seconds(key: str) -> int:
    """Seconds until the lockout on `key` lapses. 0 when it is not locked out."""
    now = time.monotonic()
    with _window_lock:
        return _refresh_lockout(key, _pruned_attempts(key, now), now)


def check_login_allowed(key: str) -> None:
    """Raise LoginThrottled when `key` has used up its failed-attempt budget."""
    limit = max_login_attempts()
    if limit <= 0:
        return
    now = time.monotonic()
    with _window_lock:
        attempts = _pruned_attempts(key, now)
        retry_after = _refresh_lockout(key, attempts, now)
    if retry_after and len(attempts) >= limit:
        raise LoginThrottled(retry_after, len(attempts))


def record_login_failure(key: str) -> int:
    """Add a failure to `key`'s window. Returns the window size afterwards, so
    the caller can tell when the limit was just reached (one audit row per
    lockout, not one per refused request)."""
    limit = max_login_attempts()
    if limit <= 0:
        return 0
    with _window_lock:
        attempts = _pruned_attempts(key, time.monotonic())
        attempts.append(time.monotonic())
        attempts = attempts[-limit:]
        _attempt_windows[key] = attempts
    return len(attempts)


def clear_login_failures(key: str) -> None:
    with _window_lock:
        _attempt_windows.pop(key, None)


def reset_login_throttle() -> None:
    with _window_lock:
        _attempt_windows.clear()


def _pruned_attempts(key: str, now: float) -> list[float]:
    window_start = now - login_lockout_seconds()
    return [timestamp for timestamp in _attempt_windows.get(key, []) if timestamp > window_start]


def _refresh_lockout(key: str, attempts: list[float], now: float) -> int:
    """Rewrite `key`'s window from pruned attempts and report seconds left.
    Caller holds _window_lock."""
    if attempts:
        _attempt_windows[key] = attempts
    else:
        _attempt_windows.pop(key, None)
    if not attempts:
        return 0
    return max(1, int(attempts[0] + login_lockout_seconds() - now) + 1)
