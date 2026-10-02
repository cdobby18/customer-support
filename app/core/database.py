import os
from collections.abc import Generator
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# Read once at import, alongside the other process-wide settings, and used by
# `init_db` so every code path agrees on what "production" means.
APP_ENV_IS_PRODUCTION = os.getenv("APP_ENV", "development").lower() == "production"

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./support.db")
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create any missing tables from the mapped classes.

    A development and test convenience only. `create_all()` never alters an
    existing table, so it cannot bring a deployed database up to date, and it
    runs DDL on every replica that boots. Production therefore leaves the
    schema to `alembic upgrade head` as a separate deploy step. Skipping it
    there also means the runtime database role only needs DML, not
    CREATE/ALTER.
    """
    if APP_ENV_IS_PRODUCTION:
        return
    Base.metadata.create_all(bind=engine)


def alembic_head_revision() -> str | None:
    """The newest revision in the migration chain, or None if it cannot be read."""
    config = Config(str(_project_root() / "alembic.ini"))
    config.set_main_option("script_location", str(_project_root() / "migrations"))
    try:
        return ScriptDirectory.from_config(config).get_current_head()
    except Exception:
        return None


def current_schema_revision(bind: Engine | None = None) -> str | None:
    """The revision recorded in the connected database, or None if unversioned.

    `bind` defaults to the process engine. It is a parameter so a caller holding
    a different engine (the test harness overrides the session, not this module)
    can ask about the database it actually talks to.
    """
    target = bind or engine
    try:
        with target.connect() as connection:
            if "alembic_version" not in inspect(connection).get_table_names():
                return None
            return connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:
        return None


def schema_is_current(bind: Engine | None = None) -> bool:
    """Whether the database is at the migration chain's head revision."""
    head = alembic_head_revision()
    return head is not None and current_schema_revision(bind) == head


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]