"""Shared FastAPI test harness.

`app.dependency_overrides` is process-global, so every test module that reaches
the API through a TestClient has to use the *same* database. Two modules each
building their own in-memory engine means whichever one was imported last wins,
and the other module's tests silently run against an empty database. Modules
import `test_engine` and `client` from here instead.

By default the database is in-memory SQLite. Set `TEST_DATABASE_URL` to point
the whole suite at a real PostgreSQL instead, which is how CI catches the
dialect differences SQLite cannot show: foreign keys that are actually enforced,
case-sensitive LIKE, and `TIMESTAMP WITH TIME ZONE` round-tripping a real
timezone. The engine is shared (a static in-memory SQLite would not be
shareable), so `reset_schema()` is the only isolation mechanism either way.
"""

import os
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

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

if TEST_DATABASE_URL:
    # A real server: pool_pre_ping because a long test run can outlive the
    # connection, and the schema is created up front because Postgres, unlike
    # an in-memory SQLite, does not conjure a database on connect.
    test_engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    Base.metadata.create_all(bind=test_engine)
else:
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
