import hashlib
import hmac
import json
import logging
import os
import time
from collections.abc import Callable
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from celery import Celery
from celery import Task

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

TASK_ALWAYS_EAGER = os.getenv("CELERY_TASK_ALWAYS_EAGER", "1").lower() in {"1", "true", "yes"}

celery_app = Celery("support", broker=REDIS_URL, backend=REDIS_URL, include=["app.core.workers"])

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


def broker_check_required() -> bool:
    """Whether readiness has to reach the broker.

    In eager mode tasks run in-process, so a replica whose Redis is down can
    still serve every request; failing its readiness would take a working API
    out of rotation. A real worker deployment does need the broker, and a
    replica that cannot enqueue a notification has no business taking traffic.
    """
    return not TASK_ALWAYS_EAGER


def broker_is_reachable(timeout: float = 1.0) -> bool:
    """Open and drop a broker connection. Never raises.

    A probe that throws turns a "not ready" into a 500, which an orchestrator
    reads as the same thing but is far harder to debug from the logs.
    """
    try:
        with celery_app.connection_for_write() as connection:
            connection.ensure_connection(max_retries=0, timeout=timeout)
        return True
    except Exception:
        return False


def notify_webhook_url() -> str:
    return os.getenv("NOTIFY_WEBHOOK_URL", "").strip()


def notify_signing_secret() -> str:
    return os.getenv("NOTIFY_WEBHOOK_SECRET", "").strip()


LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}

# 408/429 are the 4xx that can resolve on their own; a 401 from a mismatched
# signing secret cannot.
RETRYABLE_HTTP_STATUSES = {408, 429}


def notification_url_is_transport_safe(url: str) -> bool:
    """Whether `url` can carry ticket PII without leaking it in cleartext.

    HTTPS is required, with loopback exempted so the documented local setup and
    the CI job against `scripts/notify_receiver.py` still work. The inbound
    channel webhooks are HMAC-verified, so signing the outbound direction was
    the larger of the two gaps; the scheme check is what stops a misconfigured
    `http://` endpoint from silently downgrading that signature to plaintext.
    """
    parsed = urlparse(url)
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and (parsed.hostname or "") in LOOPBACK_HOSTS


def sign_notification_body(body: bytes, secret: str, timestamp: str) -> str:
    """HMAC-SHA256 over the timestamp and the exact bytes that will be sent.

    The timestamp is inside the signed material so a captured request cannot be
    replayed later; the receiver checks both the signature and that the
    timestamp is fresh. Stripe's scheme, deliberately, because it is the one
    most receivers already know how to verify.
    """
    signed = f"{timestamp}.".encode("utf-8") + body
    return hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def _post_bytes(
    url: str, body: bytes, timeout: int = 10, headers: dict | None = None
) -> int:
    """POST exactly `body`.

    `dispatch_notification` signs these same bytes, so the signed material and
    the transmitted body cannot drift apart the way they would if the body were
    serialised twice.
    """
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        return response.status


def _post_json(url: str, data: dict, timeout: int = 10, headers: dict | None = None) -> int:
    return _post_bytes(url, json.dumps(data).encode("utf-8"), timeout=timeout, headers=headers)


def notification_idempotency_key(event: str, ticket_id: str, payload: dict) -> str:
    """A stable key for one logical notification, identical across retries.

    A retried POST cannot know whether the first attempt actually reached the
    receiver, so without a key a transient failure delivers the same event up to
    four times. The key is derived from the event, ticket and payload with
    sorted keys, so every attempt (and a manual replay) produces the same value
    and the receiver can drop the duplicates.
    """
    material = json.dumps(
        {"event": event, "ticket_id": ticket_id, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


_dead_letter_sink: Callable[[dict], None] | None = None


def set_dead_letter_sink(sink: Callable[[dict], None] | None) -> None:
    """Redirect where failed notifications are recorded (tests use this).

    The default sink writes an audit row through `SessionLocal`, which in a
    test process points at the developer's `support.db` rather than the
    overridden test session, so tests inject an in-memory recorder instead.
    """
    global _dead_letter_sink
    _dead_letter_sink = sink


def record_notification_failure(
    *,
    event: str | None,
    ticket_id: str | None,
    payload: dict | None,
    error: str,
    task_id: str | None,
    attempts: int,
) -> None:
    """Persist an exhausted notification so it is visible and replayable.

    Best-effort by design: a dead letter is only useful if it never takes the
    worker down with it, so every failure here is swallowed and logged.
    """
    record = {
        "event": event,
        "ticket_id": ticket_id,
        "payload": payload,
        "error": error,
        "task_id": task_id,
        "attempts": attempts,
    }
    sink = _dead_letter_sink or _write_dead_letter_audit
    try:
        sink(record)
    except Exception:
        logger.exception("Failed to record a dead-lettered notification: %s", record)


def _write_dead_letter_audit(record: dict) -> None:
    from app.api.schemas import add_audit_log
    from app.core.database import SessionLocal
    from app.core.models import AuditLogRecord

    with SessionLocal() as db:
        add_audit_log(
            db,
            None,
            "notification.delivery_failed",
            "ticket",
            record.get("ticket_id") or "unknown",
            record,
        )
        db.commit()


class NotificationTask(Task):
    """Base task that records an audit row when a notification is given up on.

    Celery's default is to log a traceback and forget: a notification whose
    receiver was down for the whole retry budget disappears. This turns that
    terminal failure into a queryable audit row.
    """

    def on_failure(self, exc, task_id, args, kwargs, einfo):  # type: ignore[override]
        attempt = getattr(getattr(self, "request", None), "retries", 0)
        record_notification_failure(
            event=args[0] if len(args) > 0 else None,
            ticket_id=args[1] if len(args) > 1 else None,
            payload=args[2] if len(args) > 2 else None,
            error=repr(exc),
            task_id=task_id,
            attempts=attempt + 1,
        )


@celery_app.task(bind=True, base=NotificationTask, name="support.notify")
def dispatch_notification(
    self: Celery,
    event: str,
    ticket_id: str,
    payload: dict,
) -> dict:
    notify_url = notify_webhook_url()
    if not notify_url:
        return {"status": "skipped", "event": event, "ticket_id": ticket_id}

    # Config problems are returned, not raised: a bad URL will not fix itself, and
    # routing it into `self.retry` would burn three attempts and report a
    # delivery failure for what is really a deployment mistake.
    if not notification_url_is_transport_safe(notify_url):
        return {
            "status": "skipped",
            "event": event,
            "ticket_id": ticket_id,
            "reason": "notify_webhook_url_requires_https",
        }
    secret = notify_signing_secret()
    if not secret:
        return {
            "status": "skipped",
            "event": event,
            "ticket_id": ticket_id,
            "reason": "notify_webhook_secret_missing",
        }

    idempotency_key = notification_idempotency_key(event, ticket_id, payload)
    body = {
        "event": event,
        "ticket_id": ticket_id,
        "idempotency_key": idempotency_key,
        **payload,
    }
    timestamp = str(int(time.time()))
    encoded = json.dumps(body).encode("utf-8")
    signature = sign_notification_body(encoded, secret, timestamp)

    try:
        http_status = _post_bytes(
            notify_url,
            encoded,
            headers={
                "X-Support-Event": event,
                "X-Support-Timestamp": timestamp,
                "X-Support-Signature": f"t={timestamp},v1={signature}",
                "X-Support-Idempotency-Key": idempotency_key,
            },
        )
        return {
            "status": "sent",
            "event": event,
            "ticket_id": ticket_id,
            "http_status": http_status,
        }
    except HTTPError as exc:
        # Signing makes a 4xx a routine expected answer (a shared secret that
        # does not match answers 401), and retrying that three times on a fixed
        # 30 s delay just triples the noise. Only server-side and
        # "try again" statuses are worth another attempt.
        if exc.code < 500 and exc.code not in RETRYABLE_HTTP_STATUSES:
            return {
                "status": "rejected",
                "event": event,
                "ticket_id": ticket_id,
                "http_status": exc.code,
            }
        self.retry(exc=exc, countdown=30, max_retries=3)
    except Exception as exc:
        self.retry(exc=exc, countdown=30, max_retries=3)


def enqueue_notification(event: str, ticket_id: str, payload: dict) -> None:
    dispatch_notification.delay(event, ticket_id, payload)