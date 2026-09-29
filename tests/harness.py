"""Shared FastAPI test harness.

`app.dependency_overrides` is process-global, so every test module that reaches
the API through a TestClient has to use the *same* database. Two modules each
building their own in-memory engine means whichever one was imported last wins,
and the other module's tests silently run against an empty database. Modules
import `test_engine` and `client` from here instead.
"""

from collections.abc import Generator
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.models import UserRecord, UserRole
from app.main import app

ADMIN_ID = "00000000-0000-0000-0000-000000000001"
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "correct horse battery"

test_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)


def seed_admin() -> None:
    """Seed the admin account most auth/permission tests authenticate as."""
    with Session(test_engine) as session:
        session.add(
            UserRecord(
                id=ADMIN_ID,
                email=ADMIN_EMAIL,
                password_hash="unused",
                role=UserRole.admin.value,
                is_active=True,
                created_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
            )
        )
        session.commit()


def reset_schema() -> None:
    Base.metadata.drop_all(bind=test_engine)
    Base.metadata.create_all(bind=test_engine)


def drop_schema() -> None:
    Base.metadata.drop_all(bind=test_engine)


def override_get_db() -> Generator[Session, None, None]:
    with Session(test_engine) as session:
        yield session


app.dependency_overrides[get_db] = override_get_db

client = TestClient(app)
