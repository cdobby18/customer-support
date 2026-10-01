"""Startup schema handling: production must not run `create_all()`.

`create_all()` runs DDL on every replica that boots and invents tables Alembic
never declared, which is how `ticket_attachments` could ship with a model but no
migration. These tests pin the contract that production leaves the schema to
`alembic upgrade head` and that a stale or unversioned schema is reported.

`app.core.database` reads `APP_ENV` at import, so each case runs in a
subprocess with its own environment rather than mutating a module global.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run_python(code: str, **env_overrides: str) -> str:
    """Execute a snippet in a fresh interpreter and return its stdout."""
    env = dict(os.environ)
    env.update(env_overrides)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, f"subprocess failed:\n{result.stdout}\n{result.stderr}"
    return result.stdout.strip()


def test_create_all_is_skipped_in_production(tmp_path: Path) -> None:
    """Production startup must not create tables: it must not touch DDL."""
    db_path = tmp_path / "production.db"
    code = (
        "import os\n"
        "os.environ['DATABASE_URL'] = 'sqlite:///' + os.environ['TARGET_DB']\n"
        "import app.core.models  # noqa: F401  (populates Base.metadata)\n"
        "from app.core.database import init_db, engine\n"
        "from sqlalchemy import inspect\n"
        "init_db()\n"
        "print(len(inspect(engine).get_table_names()))\n"
    )
    tables_created = _run_python(
        code,
        APP_ENV="production",
        TARGET_DB=str(db_path).replace("\\", "/"),
    )
    assert tables_created == "0", "production startup ran create_all()"


def test_create_all_still_runs_outside_production(tmp_path: Path) -> None:
    """Dev, tests and the e2e job depend on create_all for zero-setup SQLite."""
    db_path = tmp_path / "development.db"
    code = (
        "import os\n"
        "os.environ['DATABASE_URL'] = 'sqlite:///' + os.environ['TARGET_DB']\n"
        "import app.core.models  # noqa: F401  (populates Base.metadata)\n"
        "from app.core.database import init_db, engine\n"
        "from sqlalchemy import inspect\n"
        "init_db()\n"
        "print(len(inspect(engine).get_table_names()))\n"
    )
    tables_created = _run_python(
        code,
        APP_ENV="development",
        TARGET_DB=str(db_path).replace("\\", "/"),
    )
    assert int(tables_created) > 0, "development startup did not create the schema"


def test_schema_revision_is_reported_as_current_after_migrating(tmp_path: Path) -> None:
    """A freshly migrated database reports itself as at the chain head."""
    db_path = tmp_path / "migrated.db"
    url = f"sqlite:///{str(db_path).replace(chr(92), '/')}"
    migrate = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "DATABASE_URL": url},
    )
    assert migrate.returncode == 0, migrate.stderr

    reported = _run_python(
        "import os\n"
        "os.environ['DATABASE_URL'] = os.environ['TARGET_URL']\n"
        "from app.core.database import schema_is_current\n"
        "print(schema_is_current())\n",
        TARGET_URL=url,
    )
    assert reported == "True"


def test_schema_revision_reports_false_when_tables_are_missing(tmp_path: Path) -> None:
    """A database created by `create_all` has no alembic_version and is not current.

    This is the state a production deploy would be in if the migration step were
    skipped, which is exactly the condition the startup warning exists to report.
    """
    db_path = tmp_path / "unversioned.db"
    code = (
        "import os\n"
        "os.environ['DATABASE_URL'] = 'sqlite:///' + os.environ['TARGET_DB']\n"
        "import app.core.models  # noqa: F401  (populates Base.metadata)\n"
        "from app.core.database import init_db, current_schema_revision, schema_is_current\n"
        "init_db()\n"
        "print(current_schema_revision())\n"
        "print(schema_is_current())\n"
    )
    output = _run_python(
        code,
        APP_ENV="development",
        TARGET_DB=str(db_path).replace("\\", "/"),
    ).splitlines()
    assert output[0] == "None", "unversioned database reported a revision"
    assert output[1] == "False"


def test_startup_warns_when_schema_is_stale(tmp_path: Path) -> None:
    """A replica on an old revision logs a warning and still serves."""
    db_path = tmp_path / "stale.db"
    url = f"sqlite:///{str(db_path).replace(chr(92), '/')}"
    # Migrate to the revision before the last one, leaving the schema behind head.
    stale = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "0012_add_revoked_tokens"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "DATABASE_URL": url},
    )
    assert stale.returncode == 0, stale.stderr

    output = _run_python(
        "import logging, os\n"
        "os.environ['DATABASE_URL'] = os.environ['TARGET_URL']\n"
        "records = []\n"
        "class Capture(logging.Handler):\n"
        "    def emit(self, record):\n"
        "        records.append(record.getMessage())\n"
        "logger = logging.getLogger('probe')\n"
        "logger.addHandler(Capture())\n"
        "logger.setLevel(logging.WARNING)\n"
        "from app.main import warn_if_schema_not_migrated\n"
        "warn_if_schema_not_migrated(logger)\n"
        "print(len(records))\n"
        "print(records[0] if records else '')\n",
        TARGET_URL=url,
    ).splitlines()

    assert output[0] == "1", f"expected one warning, got {output[0]!r}"
    assert "0012_add_revoked_tokens" in output[1]
    assert "0014_add_sessions_revoked_at" in output[1]
    assert "alembic upgrade head" in output[1]


def test_startup_does_not_warn_when_schema_is_current(tmp_path: Path) -> None:
    """A correctly migrated replica stays quiet."""
    db_path = tmp_path / "current.db"
    url = f"sqlite:///{str(db_path).replace(chr(92), '/')}"
    migrate = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "DATABASE_URL": url},
    )
    assert migrate.returncode == 0, migrate.stderr

    output = _run_python(
        "import logging, os\n"
        "os.environ['DATABASE_URL'] = os.environ['TARGET_URL']\n"
        "records = []\n"
        "class Capture(logging.Handler):\n"
        "    def emit(self, record):\n"
        "        records.append(record.getMessage())\n"
        "logger = logging.getLogger('probe2')\n"
        "logger.addHandler(Capture())\n"
        "logger.setLevel(logging.WARNING)\n"
        "from app.main import warn_if_schema_not_migrated\n"
        "warn_if_schema_not_migrated(logger)\n"
        "print(len(records))\n",
        TARGET_URL=url,
    ).splitlines()
    assert output[0] == "0", f"unexpected warning: {output}"