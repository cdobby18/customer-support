"""Schema drift guard: the Alembic chain must produce the schema the models declare.

Every other test module builds its schema from `Base.metadata` via
`create_all()`, which is why `ticket_attachments` could ship with a model but no
migration at all — nothing ever compared the two. This module is the only place
that does, so it runs the real migration chain against a throwaway SQLite
database and compares the result to `Base.metadata` table by table: columns and
indexes, not just table names.

When this fails, the fix is to write the missing migration, not to relax the
comparison. A migration that legitimately cannot match (a partial index, a
server-side default Alembic renders differently) should be excluded by name in
`_EXPECTED_DIFFERENCES` with a comment explaining why.
"""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from app.core import models as _models  # noqa: F401  (registers tables on Base)
from app.core.database import Base

# `alembic.ini` uses a relative script_location, so the chain has to be applied
# from the project root regardless of where pytest was invoked.
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Tables Alembic does not own. `alembic_version` is Alembic's own bookkeeping.
_NON_MODEL_TABLES = {"alembic_version"}

# Reserved for differences that are deliberate and cannot be removed. Kept as
# structure rather than deleted outright so that suppressing a drift is always
# an explicit, reviewable act rather than a relaxed assertion. Currently empty:
# the chain and the models agree.
#
# Example, should a future revision need one:
# _EXPECTED_DIFFERENCES = {
#     "tickets": {"columns": {"some_column"}},
# }
_EXPECTED_DIFFERENCES: dict[str, dict[str, set[str]]] = {}


@pytest.fixture(scope="module")
def migrated_inspector(tmp_path_factory: pytest.TempPathFactory):
    """Apply every revision to a fresh SQLite file and return an inspector on it.

    `migrations/env.py` reads `DATABASE_URL` from the environment, so the value
    is set for the duration and restored afterwards: a leaked override would
    silently retarget any later test that shells out to Alembic.
    """
    db_path = tmp_path_factory.mktemp("migrations") / "drift.db"
    previous_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    try:
        config = Config(str(PROJECT_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
        command.upgrade(config, "head")
    finally:
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        yield inspect(engine)
    finally:
        engine.dispose()


def _model_tables() -> set[str]:
    return set(Base.metadata.tables)


def _migrated_tables(inspector) -> set[str]:
    return set(inspector.get_table_names()) - _NON_MODEL_TABLES


def _model_columns(table: str) -> set[str]:
    return {column.name for column in Base.metadata.tables[table].columns}


def _migrated_columns(inspector, table: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table)}


def _model_indexes(table: str) -> set[frozenset[str]]:
    """The column sets each declared index covers.

    Compared by column rather than by name: the name is a convention the two
    paths have to agree on, but what an index actually does for a query is the
    columns it covers. Comparing sets of columns still catches a missing or
    extra index, which is the failure that matters, while tolerating a revision
    whose index name does not follow `ix_<table>_<column>`.
    """
    return {
        frozenset(column.name for column in index.columns)
        for index in Base.metadata.tables[table].indexes
    }


def _migrated_indexes(inspector, table: str) -> set[frozenset[str]]:
    return {
        frozenset(index.get("column_names") or ())
        for index in inspector.get_indexes(table)
    }


def _difference(table: str, kind: str) -> set[str]:
    return _EXPECTED_DIFFERENCES.get(table, {}).get(kind, set())


def test_migrations_are_not_missing_any_table(migrated_inspector) -> None:
    """Every mapped class must exist after `alembic upgrade head`.

    This is the check that would have caught the missing
    `ticket_attachments` migration: the model was declared and used by three
    endpoints, but no revision created the table.
    """
    missing = _model_tables() - _migrated_tables(migrated_inspector)
    assert not missing, (
        "tables declared in app/core/models.py but never created by a migration: "
        f"{sorted(missing)}. Add a revision, or drop the model if it is unused."
    )


def test_migrations_do_not_create_unknown_tables(migrated_inspector) -> None:
    """Migrations must not leave behind tables the models do not declare."""
    extra = _migrated_tables(migrated_inspector) - _model_tables()
    assert not extra, (
        f"tables created by the migration chain but absent from app/core/models.py: "
        f"{sorted(extra)}. A model is needed for the ORM to use them."
    )


@pytest.mark.parametrize("table", sorted(_model_tables()))
def test_table_columns_match_models(migrated_inspector, table: str) -> None:
    """Each migrated table's columns must match the mapped class exactly."""
    migrated = _migrated_columns(migrated_inspector, table) - _difference(table, "columns")
    assert migrated == _model_columns(table), (
        f"column drift in `{table}`: "
        f"missing from migrations={sorted(_model_columns(table) - migrated)}, "
        f"not in models={sorted(migrated - _model_columns(table))}"
    )


@pytest.mark.parametrize("table", sorted(_model_tables()))
def test_table_indexes_match_models(migrated_inspector, table: str) -> None:
    """Each migrated table must index exactly what the mapped class indexes.

    Compared by covered columns, not by name, so a composite index declared in
    the model matches the same composite created by a revision even if the two
    disagree on naming.
    """
    migrated = _migrated_indexes(migrated_inspector, table)
    model_indexes = _model_indexes(table)
    assert migrated == model_indexes, (
        f"index drift in `{table}`: "
        f"missing from migrations={sorted(sorted(i) for i in model_indexes - migrated)}, "
        f"not in models={sorted(sorted(i) for i in migrated - model_indexes)}"
    )