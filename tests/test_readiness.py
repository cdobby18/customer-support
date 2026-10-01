"""Tests for the liveness and readiness probes.

The old `/health` returned `{"status": "ok"}` unconditionally, so a replica
whose database was gone, or whose migrations had not been applied, still looked
healthy to every probe in the deployment. These tests pin the split:

- `/health` is liveness and must stay dependency-free, because a liveness probe
  that fails on a database blip restarts a process that was never sick.
- `/ready` is readiness and must fail (503) when the database, the schema
  revision, or the broker is not usable, so a load balancer can drain the
  replica without killing it.
"""

import pytest
from sqlalchemy import text

from app.api.routers import system
from app.core import workers
from harness import client, drop_schema, reset_schema, test_engine


@pytest.fixture(autouse=True)
def reset_database():
    reset_schema()
    yield
    drop_schema()


def test_liveness_is_static_even_when_dependencies_are_down(monkeypatch) -> None:
    monkeypatch.setattr(system, "_database_is_reachable", lambda db: False)
    monkeypatch.setattr(system, "_schema_is_current", lambda db: False)
    monkeypatch.setattr(system, "_broker_state", lambda: False)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_is_ok_when_the_database_and_schema_are_good(monkeypatch) -> None:
    monkeypatch.setattr(system, "_schema_is_current", lambda db: True)
    monkeypatch.setattr(system, "_broker_state", lambda: None)

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {"database": "ok", "schema": "ok", "broker": "skipped"},
    }


def test_readiness_fails_when_the_database_is_unreachable(monkeypatch) -> None:
    monkeypatch.setattr(system, "_database_is_reachable", lambda db: False)

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["checks"]["database"] == "failed"


def test_readiness_fails_when_the_schema_is_not_migrated(monkeypatch) -> None:
    monkeypatch.setattr(system, "_schema_is_current", lambda db: False)

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"
    assert response.json()["checks"]["schema"] == "failed"


def test_readiness_fails_when_the_broker_is_required_and_down(monkeypatch) -> None:
    monkeypatch.setattr(system, "_schema_is_current", lambda db: True)
    monkeypatch.setattr(system, "_broker_state", lambda: False)

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["checks"]["broker"] == "failed"


def test_readiness_reports_the_broker_when_it_is_reachable(monkeypatch) -> None:
    monkeypatch.setattr(system, "_schema_is_current", lambda db: True)
    monkeypatch.setattr(system, "_broker_state", lambda: True)

    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["checks"]["broker"] == "ok"


def test_broker_check_is_skipped_in_eager_mode(monkeypatch) -> None:
    monkeypatch.setattr(workers, "TASK_ALWAYS_EAGER", True)

    assert workers.broker_check_required() is False


def test_broker_check_is_required_outside_eager_mode(monkeypatch) -> None:
    monkeypatch.setattr(workers, "TASK_ALWAYS_EAGER", False)

    assert workers.broker_check_required() is True


def test_schema_revision_is_read_from_the_given_engine() -> None:
    """The override matters: readiness has to ask about the database it uses."""
    from app.core.database import current_schema_revision

    assert current_schema_revision(test_engine) is None

    try:
        with test_engine.begin() as connection:
            connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
            connection.execute(text("INSERT INTO alembic_version VALUES ('abc123')"))

        assert current_schema_revision(test_engine) == "abc123"
    finally:
        # Not part of Base.metadata, so drop_schema() would leave it behind.
        with test_engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
