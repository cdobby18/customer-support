"""Create the staff account the Playwright e2e suite logs in as.

Registration always creates a customer, so an admin has to be seeded directly.
Run this after the API has come up (its lifespan creates the schema):

    python scripts/seed_e2e_admin.py

Credentials come from E2E_ADMIN_EMAIL / E2E_ADMIN_PASSWORD so CI and a local
run agree without hard-coding them into the test file.
"""

import os
import sys
from datetime import datetime, timezone
from uuid import uuid4

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.core.database import SessionLocal, init_db  # noqa: E402
from app.core.models import UserRecord, UserRole  # noqa: E402
from app.security.auth import hash_password  # noqa: E402

EMAIL = os.getenv("E2E_ADMIN_EMAIL", "e2e-admin@relay.dev")
PASSWORD = os.getenv("E2E_ADMIN_PASSWORD", "e2e-admin-password")


def main() -> None:
    init_db()
    with SessionLocal() as session:
        existing = session.scalars(
            select(UserRecord).where(UserRecord.email == EMAIL)
        ).one_or_none()
        if existing is not None:
            print(f"admin already present: {EMAIL}")
            return
        session.add(
            UserRecord(
                id=str(uuid4()),
                email=EMAIL,
                password_hash=hash_password(PASSWORD),
                role=UserRole.admin.value,
                is_active=True,
                created_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
    print(f"seeded e2e admin: {EMAIL}")


if __name__ == "__main__":
    main()
