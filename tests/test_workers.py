import hashlib
import hmac
import importlib.util
import json
import time
from pathlib import Path

import pytest
from celery.exceptions import Retry
from urllib.error import HTTPError

from app.core import workers

_RECEIVER = Path(__file__).resolve().parents[1] / "scripts" / "notify_receiver.py"


def _load_receiver():
    spec = importlib.util.spec_from_file_location("notify_receiver", _RECEIVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sent(monkeypatch: pytest.MonkeyPatch, **env: str) -> dict:
    """Run one eager dispatch with `_post_bytes` captured, return what was sent."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    captured: dict = {}

    def fake_post_bytes(url, body, timeout=10, headers=None):
        captured.update(url=url, body=body, headers=headers or {})
        return 200

    monkeypatch.setattr(workers, "_post_bytes", fake_post_bytes)
    result = workers.dispatch_notification.apply(
        args=["ticket.created", "ticket-1", {"customer_id": "user-1"}]
    ).get()
    captured["result"] = result
    return captured


def test_celery_configured_eager_by_default() -> None:
    assert workers.celery_app.conf.task_always_eager is True
    assert workers.celery_app.conf.task_eager_propagates is True
    assert workers.REDIS_URL.startswith("redis://")


def test_notification_skipped_without_webhook_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTIFY_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("NOTIFY_WEBHOOK_SECRET", raising=False)

    result = workers.dispatch_notification.apply(
        args=["ticket.created", "ticket-1", {"customer_id": "user-1"}]
    ).get()

    assert result == {"status": "skipped", "event": "ticket.created", "ticket_id": "ticket-1"}


def test_notification_posts_payload_when_url_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _sent(
        monkeypatch,
        NOTIFY_WEBHOOK_URL="https://hooks.example.com/inbound",
        NOTIFY_WEBHOOK_SECRET="shhh",
    )

    assert sent["result"] == {
        "status": "sent",
        "event": "ticket.created",
        "ticket_id": "ticket-1",
        "http_status": 200,
    }
    assert sent["url"] == "https://hooks.example.com/inbound"
    body = json.loads(sent["body"])
    assert body["event"] == "ticket.created"
    assert body["ticket_id"] == "ticket-1"
    assert body["customer_id"] == "user-1"


def test_notification_is_signed_with_timestamp_and_hmac(monkeypatch: pytest.MonkeyPatch) -> None:
    """The body carries ticket PII, so it must not go out unauthenticated.

    The inbound channel webhooks are HMAC-verified; this closes the same gap in
    the outbound direction.
    """
    sent = _sent(
        monkeypatch,
        NOTIFY_WEBHOOK_URL="https://hooks.example.com/inbound",
        NOTIFY_WEBHOOK_SECRET="signing-secret",
    )

    headers = sent["headers"]
    assert headers["X-Support-Event"] == "ticket.created"
    timestamp = headers["X-Support-Timestamp"]
    assert abs(time.time() - int(timestamp)) < 30

    scheme = headers["X-Support-Signature"]
    prefix, _, provided = scheme.partition("v1=")
    assert prefix == f"t={timestamp},"

    expected = hmac.new(
        b"signing-secret",
        f"{timestamp}.".encode("utf-8") + sent["body"],
        hashlib.sha256,
    ).hexdigest()
    assert hmac.compare_digest(expected, provided)


def test_notification_signature_covers_the_exact_bytes_transmitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sign then verify against the stand-in receiver's own check.

    A mismatch here means the receiver would reject every real notification,
    which unit tests that only inspect our own output would not catch.
    """
    receiver = _load_receiver()
    sent = _sent(
        monkeypatch,
        NOTIFY_WEBHOOK_URL="https://hooks.example.com/inbound",
        NOTIFY_WEBHOOK_SECRET="shared-secret",
    )

    ok, reason = receiver.verify(sent["body"], sent["headers"]["X-Support-Signature"], "shared-secret")

    assert ok, reason


def test_notification_signature_rejects_a_tampered_body() -> None:
    receiver = _load_receiver()
    body = json.dumps({"event": "ticket.created", "customer_id": "user-1"}).encode("utf-8")
    timestamp = str(int(time.time()))
    signature = workers.sign_notification_body(body, "shared-secret", timestamp)
    header = f"t={timestamp},v1={signature}"

    tampered = json.dumps({"event": "ticket.created", "customer_id": "user-2"}).encode("utf-8")

    ok, reason = receiver.verify(tampered, header, "shared-secret")
    assert not ok
    assert reason == "signature mismatch"


def test_notification_signature_rejects_a_different_secret() -> None:
    receiver = _load_receiver()
    body = b'{"event":"ticket.created"}'
    timestamp = str(int(time.time()))
    signature = workers.sign_notification_body(body, "shared-secret", timestamp)

    ok, reason = receiver.verify(body, f"t={timestamp},v1={signature}", "attacker-secret")

    assert not ok
    assert reason == "signature mismatch"


def test_notification_signature_rejects_a_replayed_timestamp() -> None:
    """A captured valid request must not stay replayable forever."""
    receiver = _load_receiver()
    body = b'{"event":"ticket.created"}'
    stale = str(int(time.time()) - 3600)
    signature = workers.sign_notification_body(body, "shared-secret", stale)

    ok, reason = receiver.verify(body, f"t={stale},v1={signature}", "shared-secret")

    assert not ok
    assert "too old" in reason


def test_notification_skipped_rather_than_sent_unsigned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failing closed is the point: no secret means no PII leaves the process."""
    sent = _sent(
        monkeypatch,
        NOTIFY_WEBHOOK_URL="https://hooks.example.com/inbound",
        NOTIFY_WEBHOOK_SECRET="",
    )

    assert sent["result"] == {
        "status": "skipped",
        "event": "ticket.created",
        "ticket_id": "ticket-1",
        "reason": "notify_webhook_secret_missing",
    }
    assert sent == {"result": sent["result"]}


def test_notification_skipped_rather_than_sent_over_cleartext(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misconfigured http:// endpoint must not silently downgrade the HMAC."""
    sent = _sent(
        monkeypatch,
        NOTIFY_WEBHOOK_URL="http://hooks.example.com/inbound",
        NOTIFY_WEBHOOK_SECRET="signing-secret",
    )

    assert sent["result"] == {
        "status": "skipped",
        "event": "ticket.created",
        "ticket_id": "ticket-1",
        "reason": "notify_webhook_url_requires_https",
    }
    assert sent == {"result": sent["result"]}


@pytest.mark.parametrize(
    "url",
    [
        "https://hooks.example.com/inbound",
        "http://localhost:8099/hook",
        "http://127.0.0.1:8099/hook",
    ],
)
def test_transport_safe_urls_are_accepted(url: str) -> None:
    """Loopback http stays allowed so the documented local setup keeps working."""
    assert workers.notification_url_is_transport_safe(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.com/inbound",
        "ftp://hooks.example.com/inbound",
        "http://10.0.0.5/inbound",
        "http://169.254.169.254/inbound",
        "",
    ],
)
def test_unsafe_urls_are_rejected(url: str) -> None:
    assert workers.notification_url_is_transport_safe(url) is False


def _http_error(code: int) -> HTTPError:
    return HTTPError("https://hooks.example.com/inbound", code, "boom", {}, None)


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
def test_client_error_from_receiver_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """A rejected signature answers 401 forever; three retries only triple it."""
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://hooks.example.com/inbound")
    monkeypatch.setenv("NOTIFY_WEBHOOK_SECRET", "signing-secret")
    monkeypatch.setattr(workers, "_post_bytes", lambda *a, **k: (_ for _ in ()).throw(_http_error(code)))

    result = workers.dispatch_notification.apply(
        args=["ticket.created", "ticket-1", {"customer_id": "user-1"}]
    ).get()

    assert result == {
        "status": "rejected",
        "event": "ticket.created",
        "ticket_id": "ticket-1",
        "http_status": code,
    }


@pytest.mark.parametrize("code", [429, 500, 502, 503])
def test_transient_failure_still_retries(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://hooks.example.com/inbound")
    monkeypatch.setenv("NOTIFY_WEBHOOK_SECRET", "signing-secret")
    monkeypatch.setattr(workers, "_post_bytes", lambda *a, **k: (_ for _ in ()).throw(_http_error(code)))

    with pytest.raises(Retry):
        workers.dispatch_notification.apply(
            args=["ticket.created", "ticket-1", {"customer_id": "user-1"}]
        ).get()


def test_notification_retry_configured() -> None:
    assert workers.dispatch_notification.max_retries == 3
    assert workers.dispatch_notification.rate_limit is None


def test_enqueue_notification_runs_eagerly_without_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTIFY_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("NOTIFY_WEBHOOK_SECRET", raising=False)

    workers.enqueue_notification("ticket.created", "ticket-1", {"customer_id": "user-1"})


def test_post_json_sends_single_json_body() -> None:
    request_url: list = []
    request_method: list = []
    request_headers: list = []
    request_body: list = []

    class FakeResponse:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    original_urlopen = workers.urlopen

    def fake_urlopen(request, timeout=10):
        request_url.append(request.full_url)
        request_method.append(request.method)
        request_headers.append(request.headers.get("Content-type"))
        request_body.append(request.data)
        return FakeResponse()

    workers.urlopen = fake_urlopen
    try:
        status = workers._post_json("https://hooks.example.com/inbound", {"a": 1})
    finally:
        workers.urlopen = original_urlopen

    assert status == 202
    assert request_url[0] == "https://hooks.example.com/inbound"
    assert request_method[0] == "POST"
    assert request_headers[0] == "application/json"
    assert b'"a"' in request_body[0]


# --- dead letters and idempotency (#23) ------------------------------------


def test_idempotency_key_is_stable_and_content_addressed() -> None:
    key = workers.notification_idempotency_key(
        "ticket.created", "ticket-1", {"customer_id": "u1"}
    )
    again = workers.notification_idempotency_key(
        "ticket.created", "ticket-1", {"customer_id": "u1"}
    )
    other = workers.notification_idempotency_key(
        "ticket.created", "ticket-1", {"customer_id": "u2"}
    )

    assert key == again
    assert key != other
    assert len(key) == 64


def test_idempotency_key_is_independent_of_payload_order() -> None:
    a = workers.notification_idempotency_key("ticket.created", "t", {"a": 1, "b": 2})
    b = workers.notification_idempotency_key("ticket.created", "t", {"b": 2, "a": 1})

    assert a == b


def test_notification_sends_the_idempotency_key(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _sent(
        monkeypatch,
        NOTIFY_WEBHOOK_URL="https://hooks.example.com/inbound",
        NOTIFY_WEBHOOK_SECRET="shhh",
    )

    body = json.loads(sent["body"])
    header = sent["headers"]["X-Support-Idempotency-Key"]

    assert header == body["idempotency_key"]
    assert header == workers.notification_idempotency_key(
        "ticket.created", "ticket-1", {"customer_id": "user-1"}
    )


def test_on_failure_records_a_dead_letter(monkeypatch: pytest.MonkeyPatch) -> None:
    records: list[dict] = []
    monkeypatch.setattr(workers, "_dead_letter_sink", records.append)

    workers.dispatch_notification.on_failure(
        RuntimeError("receiver down"),
        "task-abc",
        ["ticket.created", "ticket-1", {"customer_id": "u1"}],
        {},
        None,
    )

    assert len(records) == 1
    record = records[0]
    assert record["event"] == "ticket.created"
    assert record["ticket_id"] == "ticket-1"
    assert "receiver down" in record["error"]
    assert record["task_id"] == "task-abc"
    assert record["attempts"] == 1


def test_a_failing_dead_letter_sink_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(record: dict) -> None:
        raise RuntimeError("audit store down")

    monkeypatch.setattr(workers, "_dead_letter_sink", boom)

    # Must never take the worker down with it.
    workers.record_notification_failure(
        event="ticket.created",
        ticket_id="ticket-1",
        payload={},
        error="x",
        task_id="task-abc",
        attempts=4,
    )


def test_default_dead_letter_sink_writes_an_audit_row(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api import schemas
    from app.core import database

    calls: list = []

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def commit(self):
            calls.append("commit")

    def fake_add_audit_log(db, actor_id, action, entity_type, entity_id, details):
        calls.append((actor_id, action, entity_type, entity_id, details))

    monkeypatch.setattr(database, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(schemas, "add_audit_log", fake_add_audit_log)

    workers._write_dead_letter_audit(
        {
            "event": "ticket.created",
            "ticket_id": "ticket-1",
            "payload": {"customer_id": "u1"},
            "error": "boom",
            "task_id": "task-abc",
            "attempts": 4,
        }
    )

    assert calls[0][0] is None
    assert calls[0][1] == "notification.delivery_failed"
    assert calls[0][2] == "ticket"
    assert calls[0][3] == "ticket-1"
    assert calls[1] == "commit"