import pytest

from app.core import workers


def test_celery_configured_eager_by_default() -> None:
    assert workers.celery_app.conf.task_always_eager is True
    assert workers.celery_app.conf.task_eager_propagates is True
    assert workers.REDIS_URL.startswith("redis://")


def test_notification_skipped_without_webhook_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTIFY_WEBHOOK_URL", raising=False)

    result = workers.dispatch_notification.apply(
        args=["ticket.created", "ticket-1", {"customer_id": "user-1"}]
    ).get()

    assert result == {"status": "skipped", "event": "ticket.created", "ticket_id": "ticket-1"}


def test_notification_posts_payload_when_url_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://hooks.example.com/inbound")
    sent: dict = {}
    monkeypatch.setattr(
        workers,
        "_post_json",
        lambda url, data, timeout=10: sent.update(url=url, data=data) or 200,
    )

    result = workers.dispatch_notification.apply(
        args=["ticket.created", "ticket-1", {"customer_id": "user-1"}]
    ).get()

    assert result == {
        "status": "sent",
        "event": "ticket.created",
        "ticket_id": "ticket-1",
        "http_status": 200,
    }
    assert sent["url"] == "https://hooks.example.com/inbound"
    assert sent["data"]["event"] == "ticket.created"
    assert sent["data"]["ticket_id"] == "ticket-1"
    assert sent["data"]["customer_id"] == "user-1"


def test_notification_retry_configured() -> None:
    assert workers.dispatch_notification.max_retries == 3
    assert workers.dispatch_notification.rate_limit is None


def test_enqueue_notification_runs_eagerly_without_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTIFY_WEBHOOK_URL", raising=False)

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