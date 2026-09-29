"""Login throttling and access-token revocation.

The throttle is process-local module state, so every test resets it; the
revocation rows live in the test database, which the fixture recreates.
"""

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.models import AuditLogRecord, RevokedTokenRecord
from app.security import login_throttle
from app.security.auth import JWT_ALGORITHM, JWT_SECRET, create_access_token
from app.security.login_throttle import (
    LoginThrottled,
    check_login_allowed,
    clear_login_failures,
    login_lockout_seconds,
    login_throttle_key,
    max_login_attempts,
    record_login_failure,
    reset_login_throttle,
)
from app.security.sessions import purge_expired_revocations
from harness import ADMIN_ID, client, drop_schema, reset_schema, seed_admin, test_engine

PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.setenv("AUTH_LOGIN_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("AUTH_LOGIN_LOCKOUT_SECONDS", "300")
    reset_schema()
    seed_admin()
    reset_login_throttle()
    yield
    reset_login_throttle()
    drop_schema()


def register_customer(email: str = "customer@example.com") -> None:
    response = client.post("/auth/register", json={"email": email, "password": PASSWORD})
    assert response.status_code == 201


def login(email: str = "customer@example.com", password: str = PASSWORD):
    return client.post("/auth/login", json={"email": email, "password": password})


def audit_actions(action: str) -> list[AuditLogRecord]:
    with Session(test_engine) as session:
        return list(
            session.scalars(
                select(AuditLogRecord).where(AuditLogRecord.action == action)
            ).all()
        )


def test_login_throttle_key_is_case_and_whitespace_insensitive() -> None:
    assert login_throttle_key("  Admin@Example.com ", "10.0.0.1") == login_throttle_key(
        "admin@example.com", "10.0.0.1"
    )


def test_login_throttle_key_separates_client_hosts() -> None:
    assert login_throttle_key("admin@example.com", "10.0.0.1") != login_throttle_key(
        "admin@example.com", "10.0.0.2"
    )


def test_limit_and_lockout_read_env_with_defaults(monkeypatch) -> None:
    monkeypatch.delenv("AUTH_LOGIN_MAX_ATTEMPTS", raising=False)
    monkeypatch.delenv("AUTH_LOGIN_LOCKOUT_SECONDS", raising=False)
    assert max_login_attempts() == login_throttle.DEFAULT_MAX_ATTEMPTS
    assert login_lockout_seconds() == login_throttle.DEFAULT_LOCKOUT_SECONDS


def test_non_numeric_limit_falls_back_to_default(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_LOGIN_MAX_ATTEMPTS", "not-a-number")
    assert max_login_attempts() == login_throttle.DEFAULT_MAX_ATTEMPTS


def test_non_numeric_lockout_falls_back_to_default(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_LOGIN_LOCKOUT_SECONDS", "not-a-number")
    assert login_lockout_seconds() == login_throttle.DEFAULT_LOCKOUT_SECONDS


def test_failed_attempts_lock_out_after_the_limit() -> None:
    key = login_throttle_key("admin@example.com", "testclient")

    check_login_allowed(key)
    assert record_login_failure(key) == 1
    check_login_allowed(key)
    assert record_login_failure(key) == 2
    check_login_allowed(key)
    assert record_login_failure(key) == 3

    with pytest.raises(LoginThrottled) as throttled:
        check_login_allowed(key)
    assert throttled.value.attempts == 3
    assert 0 < throttled.value.retry_after <= 300


def test_lockout_lapses_once_the_window_expires(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_LOGIN_LOCKOUT_SECONDS", "1")
    key = login_throttle_key("admin@example.com", "testclient")
    for _ in range(3):
        record_login_failure(key)

    monkeypatch.setattr(login_throttle.time, "monotonic", lambda: login_throttle.time.perf_counter() + 5)
    check_login_allowed(key)
    assert login_throttle.retry_after_seconds(key) == 0


def test_successful_login_clears_the_window() -> None:
    key = login_throttle_key("admin@example.com", "testclient")
    for _ in range(2):
        record_login_failure(key)
    clear_login_failures(key)
    check_login_allowed(key)


def test_reset_clears_every_window() -> None:
    key = login_throttle_key("admin@example.com", "testclient")
    for _ in range(3):
        record_login_failure(key)
    reset_login_throttle()
    check_login_allowed(key)


def test_throttle_is_disabled_when_the_limit_is_zero(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_LOGIN_MAX_ATTEMPTS", "0")
    key = login_throttle_key("admin@example.com", "testclient")
    for _ in range(10):
        assert record_login_failure(key) == 0
    check_login_allowed(key)


def test_login_returns_429_after_the_limit_with_retry_after() -> None:
    register_customer()
    for _ in range(3):
        assert login(password="wrong password").status_code == 401

    response = login()

    assert response.status_code == 429
    assert response.json()["detail"] == "Too many failed login attempts. Try again later."
    assert 0 < int(response.headers["Retry-After"]) <= 300


def test_correct_password_is_refused_while_locked_out() -> None:
    register_customer()
    for _ in range(3):
        login(password="wrong password")

    assert login().status_code == 429


def test_lockout_is_audited_once_not_on_every_refused_attempt() -> None:
    register_customer()
    for _ in range(3):
        assert login(password="wrong password").status_code == 401
    for _ in range(3):
        assert login().status_code == 429

    lockouts = audit_actions("auth.login_locked")
    assert len(lockouts) == 1
    assert lockouts[0].entity_id == "customer@example.com"
    assert lockouts[0].details["failed_attempts"] == 3
    assert lockouts[0].details["client_host"] == "testclient"


def test_successful_login_resets_the_lockout_counter() -> None:
    register_customer()
    for _ in range(2):
        login(password="wrong password")

    assert login().status_code == 200
    for _ in range(2):
        login(password="wrong password")
    assert login().status_code == 200


def test_throttle_is_scoped_per_email() -> None:
    register_customer("one@example.com")
    for _ in range(3):
        login("one@example.com", password="wrong password")

    assert login("one@example.com").status_code == 429
    assert login("two@example.com").status_code == 401


def test_logout_revokes_the_presented_token() -> None:
    register_customer()
    token = login().json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/auth/me", headers=headers).status_code == 200

    response = client.post("/auth/logout", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"revoked": True}
    assert client.get("/auth/me", headers=headers).status_code == 401
    assert client.get("/auth/me", headers=headers).json()["detail"] == "Token has been revoked"


def test_logout_is_idempotent() -> None:
    register_customer()
    headers = {"Authorization": f"Bearer {login().json()['access_token']}"}

    assert client.post("/auth/logout", headers=headers).json() == {"revoked": True}
    assert client.post("/auth/logout", headers=headers).json() == {"revoked": False}
    assert len(audit_actions("auth.logout")) == 2


def test_logout_only_revokes_its_own_session() -> None:
    register_customer()
    first = {"Authorization": f"Bearer {login().json()['access_token']}"}
    second = {"Authorization": f"Bearer {login().json()['access_token']}"}

    assert client.post("/auth/logout", headers=first).status_code == 200

    assert client.get("/auth/me", headers=first).status_code == 401
    assert client.get("/auth/me", headers=second).status_code == 200


def test_logout_requires_a_token() -> None:
    assert client.post("/auth/logout").status_code == 401


def test_logout_rejects_a_forged_token() -> None:
    forged = jwt.encode(
        {"sub": ADMIN_ID, "jti": "forged", "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
        "wrong-secret",
        algorithm=JWT_ALGORITHM,
    )

    response = client.post("/auth/logout", headers={"Authorization": f"Bearer {forged}"})

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid or expired token"


def test_logout_audit_records_the_actor() -> None:
    register_customer()
    token = login().json()["access_token"]

    client.post("/auth/logout", headers={"Authorization": f"Bearer {token}"})

    records = audit_actions("auth.logout")
    assert records[0].actor_id is not None
    assert records[0].details == {"revoked": True}


def test_revoke_row_is_dropped_once_the_token_expires() -> None:
    with Session(test_engine) as session:
        session.add(
            RevokedTokenRecord(
                session_id="expired-session",
                user_id=ADMIN_ID,
                expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                revoked_at=datetime.now(timezone.utc) - timedelta(hours=2),
            )
        )
        session.commit()

    with Session(test_engine) as session:
        assert purge_expired_revocations(session) == 1
        session.commit()

    with Session(test_engine) as session:
        assert session.get(RevokedTokenRecord, "expired-session") is None


def test_revoking_keeps_unexpired_rows() -> None:
    with Session(test_engine) as session:
        session.add(
            RevokedTokenRecord(
                session_id="live-session",
                user_id=ADMIN_ID,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
                revoked_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
        assert purge_expired_revocations(session) == 0


def test_token_without_a_session_id_is_rejected() -> None:
    legacy = jwt.encode(
        {"sub": ADMIN_ID, "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )

    response = client.get("/auth/me", headers={"Authorization": f"Bearer {legacy}"})

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid or expired token"


def test_token_without_an_expiry_is_rejected() -> None:
    # PyJWT treats `exp` as optional, so without this check the token would be
    # valid forever and unrevokable in practice.
    forever = jwt.encode({"sub": ADMIN_ID, "jti": "no-expiry"}, JWT_SECRET, algorithm=JWT_ALGORITHM)

    response = client.get("/auth/me", headers={"Authorization": f"Bearer {forever}"})

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid or expired token"


def test_token_without_a_subject_is_rejected() -> None:
    subjectless = jwt.encode(
        {"jti": "no-subject", "exp": datetime.now(timezone.utc) + timedelta(minutes=5)},
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )

    response = client.get("/auth/me", headers={"Authorization": f"Bearer {subjectless}"})

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid or expired token"


def test_access_token_carries_a_session_id() -> None:
    claims = jwt.decode(
        create_access_token(ADMIN_ID), JWT_SECRET, algorithms=[JWT_ALGORITHM]
    )
    assert claims["sub"] == ADMIN_ID
    assert claims["jti"]
