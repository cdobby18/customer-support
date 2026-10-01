"""Login throttling and access-token revocation.

The throttle is process-local module state, so every test resets it; the
revocation rows live in the test database, which the fixture recreates.
"""

import time
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.models import AuditLogRecord, RevokedTokenRecord
from app.security import login_throttle
from app.security.auth import (
    ACCESS_TOKEN_TYPE,
    JWT_ALGORITHM,
    JWT_SECRET,
    create_access_token,
    decode_access_token,
)
from app.security import rate_limit
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

    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: rate_limit.time.perf_counter() + 5)
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
    raw = jwt.decode(
        create_access_token(ADMIN_ID),
        JWT_SECRET,
        algorithms=[JWT_ALGORITHM],
        audience="ai-customer-support-api",
    )
    assert raw["sub"] == ADMIN_ID
    assert raw["jti"]
    assert raw["iss"] == "ai-customer-support"
    assert raw["typ"] == ACCESS_TOKEN_TYPE
    assert raw["iat"]

    claims = decode_access_token(create_access_token(ADMIN_ID))
    assert claims.user_id == ADMIN_ID
    assert claims.issued_at


# --- token claims and secret rotation (#18) -------------------------------


def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(ADMIN_ID)}"}


def test_token_from_another_issuer_is_rejected(monkeypatch) -> None:
    token = create_access_token(ADMIN_ID)
    monkeypatch.setenv("JWT_ISSUER", "some-other-service")

    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


def test_token_for_another_audience_is_rejected(monkeypatch) -> None:
    token = create_access_token(ADMIN_ID)
    monkeypatch.setenv("JWT_AUDIENCE", "some-other-api")

    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


def test_token_with_the_wrong_type_is_rejected() -> None:
    from app.security.auth import jwt_audience, jwt_issuer, jwt_secret

    now = datetime.now(timezone.utc)
    refresh = jwt.encode(
        {
            "sub": ADMIN_ID,
            "jti": "refresh-token",
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "iss": jwt_issuer(),
            "aud": jwt_audience(),
            "typ": "refresh",
        },
        jwt_secret(),
        algorithm=JWT_ALGORITHM,
    )

    response = client.get("/auth/me", headers={"Authorization": f"Bearer {refresh}"})

    assert response.status_code == 401


def test_the_signing_secret_is_read_at_call_time(monkeypatch) -> None:
    monkeypatch.setenv("JWT_SECRET", "a-rotated-secret-that-is-long-enough")

    token = create_access_token(ADMIN_ID)

    # Signed with the rotated value, not the one snapshotted at import.
    decoded = jwt.decode(
        token,
        "a-rotated-secret-that-is-long-enough",
        algorithms=[JWT_ALGORITHM],
        audience="ai-customer-support-api",
        issuer="ai-customer-support",
    )
    assert decoded["sub"] == ADMIN_ID


# --- registration gating and throttling (#19) -----------------------------


def test_registration_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_REGISTRATION_ENABLED", "0")

    response = client.post(
        "/auth/register", json={"email": "nobody@example.com", "password": PASSWORD}
    )

    assert response.status_code == 403


def test_registration_is_throttled_per_client_host(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_REGISTER_MAX_ATTEMPTS", "2")

    assert client.post(
        "/auth/register", json={"email": "one@example.com", "password": PASSWORD}
    ).status_code == 201
    assert client.post(
        "/auth/register", json={"email": "two@example.com", "password": PASSWORD}
    ).status_code == 201

    blocked = client.post(
        "/auth/register", json={"email": "three@example.com", "password": PASSWORD}
    )

    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"]


def test_duplicate_registration_is_still_a_conflict(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_REGISTER_MAX_ATTEMPTS", "5")
    register_customer("dup@example.com")

    response = client.post(
        "/auth/register", json={"email": "dup@example.com", "password": PASSWORD}
    )

    assert response.status_code == 409


# --- per-user revoke all sessions (#24) ------------------------------------


def test_revoke_all_sessions_kills_the_calling_token() -> None:
    register_customer()
    token = login().json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    response = client.post("/auth/revoke-all-sessions", headers=headers)

    assert response.status_code == 200
    assert response.json()["revoked"] is True
    assert client.get("/auth/me", headers=headers).status_code == 401


def test_a_fresh_login_after_revoke_all_works() -> None:
    register_customer()
    first = login().json()["access_token"]
    client.post(
        "/auth/revoke-all-sessions", headers={"Authorization": f"Bearer {first}"}
    )
    # A JWT `iat` is whole seconds, so a token minted in the same second as the
    # cutoff is indistinguishable from one minted just before it and is revoked
    # too (the conservative choice). Cross the second boundary to mint a token
    # that is unambiguously later than the cutoff.
    time.sleep(1.05)

    second = login().json()["access_token"]

    assert client.get(
        "/auth/me", headers={"Authorization": f"Bearer {second}"}
    ).status_code == 200


def test_revoke_all_sessions_does_not_touch_other_users() -> None:
    register_customer("a@example.com")
    register_customer("b@example.com")
    a_token = login("a@example.com").json()["access_token"]
    b_token = login("b@example.com").json()["access_token"]

    client.post(
        "/auth/revoke-all-sessions", headers={"Authorization": f"Bearer {a_token}"}
    )

    assert client.get(
        "/auth/me", headers={"Authorization": f"Bearer {b_token}"}
    ).status_code == 200


def test_revoke_all_sessions_requires_a_token() -> None:
    assert client.post("/auth/revoke-all-sessions").status_code == 401


def test_revoke_all_sessions_is_audited() -> None:
    register_customer()
    token = login().json()["access_token"]

    client.post("/auth/revoke-all-sessions", headers={"Authorization": f"Bearer {token}"})

    records = audit_actions("auth.sessions_revoked")
    assert records
    assert records[0].details == {"scope": "self"}


def test_admin_can_revoke_another_users_sessions() -> None:
    register_customer("target@example.com")
    target = login("target@example.com").json()
    target_headers = {"Authorization": f"Bearer {target['access_token']}"}

    response = client.post(
        f"/admin/users/{target['user']['id']}/revoke-sessions", headers=admin_headers()
    )

    assert response.status_code == 200
    assert response.json()["revoked"] is True
    assert client.get("/auth/me", headers=target_headers).status_code == 401


def test_admin_revoke_sessions_requires_admin() -> None:
    register_customer("plain@example.com")
    customer_headers = {"Authorization": f"Bearer {login('plain@example.com').json()['access_token']}"}

    response = client.post(
        f"/admin/users/{ADMIN_ID}/revoke-sessions", headers=customer_headers
    )

    assert response.status_code == 403


def test_admin_revoke_sessions_for_unknown_user_is_404() -> None:
    response = client.post(
        "/admin/users/00000000-0000-0000-0000-0000000000aa/revoke-sessions",
        headers=admin_headers(),
    )

    assert response.status_code == 404
