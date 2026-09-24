import json
import os
from urllib.request import Request, urlopen

from celery import Celery

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

TASK_ALWAYS_EAGER = os.getenv("CELERY_TASK_ALWAYS_EAGER", "1").lower() in {"1", "true", "yes"}

celery_app = Celery("support", broker=REDIS_URL, backend=REDIS_URL, include=["app.workers"])

celery_app.conf.update(
    task_always_eager=TASK_ALWAYS_EAGER,
    task_eager_propagates=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    enable_utc=True,
    timezone="UTC",
    task_default_queue="support",
    task_default_exchange="support",
    task_default_routing_key="support",
    broker_connection_retry_on_startup=True,
    result_expires=86400,
)


def notify_webhook_url() -> str:
    return os.getenv("NOTIFY_WEBHOOK_URL", "").strip()


def _post_json(url: str, data: dict, timeout: int = 10) -> int:
    body = json.dumps(data).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        return response.status


@celery_app.task(bind=True, name="support.notify")
def dispatch_notification(
    self: Celery,
    event: str,
    ticket_id: str,
    payload: dict,
) -> dict:
    notify_url = notify_webhook_url()
    if not notify_url:
        return {"status": "skipped", "event": event, "ticket_id": ticket_id}
    try:
        http_status = _post_json(notify_url, {"event": event, "ticket_id": ticket_id, **payload})
        return {
            "status": "sent",
            "event": event,
            "ticket_id": ticket_id,
            "http_status": http_status,
        }
    except Exception as exc:
        self.retry(exc=exc, countdown=30, max_retries=3)


def enqueue_notification(event: str, ticket_id: str, payload: dict) -> None:
    dispatch_notification.delay(event, ticket_id, payload)