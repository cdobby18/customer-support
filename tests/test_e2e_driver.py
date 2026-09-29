"""Tests for the Task 23 end-to-end driver's own correctness.

The driver in `scripts/e2e_postgres_worker.py` exists to prove a task really
travels through a broker. It can pass for the wrong reasons, and both wrong
reasons are silent:

- The worker prints every registered task name in its startup banner, so
  "the log mentions support.notify" is true the moment the worker boots and
  proves nothing about whether it ever ran anything.
- `CELERY_TASK_ALWAYS_EAGER` defaults to 1 in this project, so a producer that
  forgets to set it to 0 runs its own task inline and passes without a broker.

These are cheap to test and expensive to discover, since the only other place
they show up is a CI job that passes for the wrong reason.
"""

import importlib.util
import os
from pathlib import Path

import pytest

_DRIVER = Path(__file__).resolve().parents[1] / "scripts" / "e2e_postgres_worker.py"


def _load_driver():
    spec = importlib.util.spec_from_file_location("e2e_postgres_worker", _DRIVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


driver = _load_driver()

# What a real Celery worker prints at startup with --loglevel=debug. The banner
# lists the task; it is not evidence of execution.
BANNER = """
[config]
.> app: 'support'
.> transport: 'redis://127.0.0.1:6379/0'
[queues]
.> exchange=support(direct) routing_key=support
[tasks]
  . support.notify
[ready]
"""


def test_banner_alone_is_not_treated_as_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The trap: a booted-but-idle worker must not pass the check."""
    log = tmp_path / "worker.log"
    log.write_text(BANNER, encoding="utf-8")
    monkeypatch.setattr(driver, "WORKER_LOG", str(log))

    assert driver.worker_received_task(offset=0) is False


def test_received_line_after_the_offset_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "worker.log"
    log.write_text(BANNER, encoding="utf-8")
    offset = log.stat().st_size
    with log.open("a", encoding="utf-8") as handle:
        handle.write("[celeryd.worker.task] Task support.notify[abc-123] received\n")
    monkeypatch.setattr(driver, "WORKER_LOG", str(log))

    assert driver.worker_received_task(offset=offset) is True


def test_received_line_before_the_offset_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A receipt from a previous run must not satisfy this run's assertion."""
    log = tmp_path / "worker.log"
    log.write_text(
        BANNER + "[celeryd.worker.task] Task support.notify[abc-123] received\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(driver, "WORKER_LOG", str(log))

    assert driver.worker_received_task(offset=log.stat().st_size) is False


def test_missing_log_is_not_a_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(driver, "WORKER_LOG", str(tmp_path / "does-not-exist.log"))

    assert driver.worker_received_task(offset=0) is False


def test_configure_as_producer_forces_non_eager(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even when the ambient environment says otherwise."""
    monkeypatch.setenv("CELERY_TASK_ALWAYS_EAGER", "1")

    driver.configure_as_producer()

    assert os.environ["CELERY_TASK_ALWAYS_EAGER"] == "0"
