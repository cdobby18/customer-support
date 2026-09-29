"""Registration, login, logout and current-user lookup."""

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, read_token_claims
from app.api.schemas import (
    LoginRequest,
    LoginResponse,
    LogoutResponse,
    RegisterRequest,
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
    clear_login_failures,
    login_throttle_key,
    max_login_attempts,
    record_login_failure,
)
from app.security.sessions import revoke_token

router = APIRouter()


def client_host(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.post("/auth/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register_user(user_data: RegisterRequest, db: Session = Depends(get_db)) -> UserResponse:
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


@router.get("/auth/me", response_model=UserResponse)
def get_authenticated_user(current_user: UserRecord = Depends(get_current_user)) -> UserResponse:
    return to_user(current_user)
