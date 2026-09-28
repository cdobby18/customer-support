"""Registration, login and current-user lookup."""

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import create_access_token, hash_password, verify_password
from app.database import get_db
from app.dependencies import get_current_user
from app.models import UserRecord, UserRole
from app.schemas import (
    LoginRequest,
    LoginResponse,
    RegisterRequest,
    UserResponse,
    add_audit_log,
    to_user,
)

router = APIRouter()


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
def login_user(user_data: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
    normalized_email = str(user_data.email).lower()
    record = db.scalar(select(UserRecord).where(UserRecord.email == normalized_email))
    if record is None or not record.is_active or not verify_password(
        user_data.password, record.password_hash
    ):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    return LoginResponse(
        authenticated=True,
        user=to_user(record),
        access_token=create_access_token(record.id),
    )


@router.get("/auth/me", response_model=UserResponse)
def get_authenticated_user(current_user: UserRecord = Depends(get_current_user)) -> UserResponse:
    return to_user(current_user)
