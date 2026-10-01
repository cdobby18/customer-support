"""Registration, login, logout and current-user lookup."""

import os
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, read_token_claims, require_staff
from app.api.schemas import (
    LoginRequest,
    LoginResponse,
    LogoutResponse,
    RegisterRequest,
    RevokedSessionsResponse,
    UserResponse,
    add_audit_log,
    to_user,
)
from app.core.database import get_db
from app.core.models import UserRecord, UserRole
from app.security.auth import (
    TokenClaims,
    create_access_token,
    hash_password,
    verify_password,
)
from app.security.login_throttle import (
    LoginThrottled,
    check_login_allowed,
    check_registration_allowed,
    clear_login_failures,
    login_throttle_key,
    max_login_attempts,
    record_login_failure,
    record_registration_attempt,
    registration_throttle_key,
)
from app.security.sessions import revoke_all_sessions, revoke_token

router = APIRouter()


def client_host(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def registration_enabled() -> bool:
    """Public self-service registration can be switched off for locked-down deploys."""
    return os.getenv("AUTH_REGISTRATION_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }


@router.post("/auth/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register_user(
    user_data: RegisterRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> UserResponse:
    if not registration_enabled():
        raise HTTPException(status_code=403, detail="Public registration is disabled")

    # Unauthenticated write path: throttle per client host before doing any
    # work, so account creation and the duplicate-email probe cannot be run at
    # request speed.
    throttle_key = registration_throttle_key(client_host(request))
    try:
        check_registration_allowed(throttle_key)
    except LoginThrottled as throttled:
        raise HTTPException(
            status_code=429,
            detail="Too many registration attempts. Try again later.",
            headers={"Retry-After": str(throttled.retry_after)},
        ) from None
    record_registration_attempt(throttle_key)

    normalized_email = str(user_data.email).lower()
    existing_user = db.scalar(select(UserRecord).where(UserRecord.email == normalized_email))
    if existing_user is not None:
        raise HTTPException(status_code=409, detail="Email is already registered")

    record = UserRecord(
        id=str(uuid4()),
        email=normalized_email,
        password_hash=hash_password(user_data.password),
        role=UserRole.customer.value,
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(record)
    add_audit_log(db, None, "user.created", "user", record.id, {"role": record.role})
    db.commit()
    db.refresh(record)
    return to_user(record)


@router.post("/auth/login", response_model=LoginResponse)
def login_user(
    user_data: LoginRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> LoginResponse:
    normalized_email = str(user_data.email).lower()
    host = client_host(request)
    throttle_key = login_throttle_key(normalized_email, host)
    try:
        check_login_allowed(throttle_key)
    except LoginThrottled as throttled:
        raise HTTPException(
            status_code=429,
            detail="Too many failed login attempts. Try again later.",
            headers={"Retry-After": str(throttled.retry_after)},
        ) from None

    record = db.scalar(select(UserRecord).where(UserRecord.email == normalized_email))
    if record is None or not record.is_active or not verify_password(
        user_data.password, record.password_hash
    ):
        failed_attempts = record_login_failure(throttle_key)
        if failed_attempts >= max_login_attempts():
            # Audited once per lockout window, not once per refused request.
            add_audit_log(
                db,
                None,
                "auth.login_locked",
                "user",
                normalized_email,
                {"client_host": host, "failed_attempts": failed_attempts},
            )
            db.commit()
        raise HTTPException(status_code=401, detail="Invalid email or password")
    clear_login_failures(throttle_key)
    return LoginResponse(
        authenticated=True,
        user=to_user(record),
        access_token=create_access_token(record.id),
    )


@router.post("/auth/logout", response_model=LogoutResponse)
def logout_user(
    claims: TokenClaims = Depends(read_token_claims),
    db: Session = Depends(get_db),
) -> LogoutResponse:
    """Revoke the presented token. Idempotent: revoking twice is still 200."""
    revoked = revoke_token(
        db,
        session_id=claims.session_id,
        user_id=claims.user_id,
        expires_at=claims.expires_at,
    )
    add_audit_log(db, claims.user_id, "auth.logout", "user", claims.user_id, {"revoked": revoked})
    db.commit()
    return LogoutResponse(revoked=revoked)


@router.post("/auth/revoke-all-sessions", response_model=RevokedSessionsResponse)
def revoke_own_sessions(
    claims: TokenClaims = Depends(read_token_claims),
    db: Session = Depends(get_db),
) -> RevokedSessionsResponse:
    """Log this account out everywhere.

    Logout revokes one `jti`; this sets a cutoff that also kills sessions this
    process never issued a revocation row for. The token used to call it is
    itself minted at or before the cutoff, so it stops working too.
    """
    cutoff = revoke_all_sessions(db, user_id=claims.user_id)
    add_audit_log(
        db,
        claims.user_id,
        "auth.sessions_revoked",
        "user",
        claims.user_id,
        {"scope": "self"},
    )
    db.commit()
    return RevokedSessionsResponse(revoked=cutoff is not None, revoked_at=cutoff)


@router.get("/auth/me", response_model=UserResponse)
def get_authenticated_user(current_user: UserRecord = Depends(get_current_user)) -> UserResponse:
    return to_user(current_user)


@router.get("/users/staff", response_model=list[UserResponse])
def list_staff_assignees(
    _: UserRecord = Depends(require_staff),
    db: Session = Depends(get_db),
) -> list[UserResponse]:
    """Roster of assignable staff (agents and admins) for anyone working the queue."""
    query = (
        select(UserRecord)
        .where(
            UserRecord.role.in_([UserRole.agent.value, UserRole.admin.value]),
            UserRecord.is_active.is_(True),
        )
        .order_by(UserRecord.created_at)
    )
    return [to_user(record) for record in db.scalars(query).all()]
