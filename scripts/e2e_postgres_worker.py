"""End-to-end proof: a real Celery task, executed by a real worker, off a real
broker, doing real work against real PostgreSQL.

Until now `enqueue_notification` was monkeypatched in every test, so `.delay()`
had never been called against a broker that was actually there. The worker was
only ever proven to *register* a task name, which is not the same as proving it
runs one.

Two phases, because they fail for different reasons and are worth separating:

1. **App-level.** Register a customer, promote them to admin directly in
   PostgreSQL, log in for a JWT, create a ticket, and wait for the outbound
   webhook. This proves the request path that production uses end to end:
   register -> DB write -> login -> JWT -> staff check -> ticket insert ->
   commit -> enqueue -> broker -> worker -> HTTP callback.

2. **Task-level.** Enqueue a synthetic event with `apply_async` and read the
   return value back out of the Redis result backend. This proves the part
   phase 1 cannot see: that the task's *return value* makes the round trip,
   which is what a caller would actually consume.

Run against services the caller has already started:

    python scripts/e2e_postgres_worker.py --api http://127.0.0.1:8000 \\
        --receiver http://127.0.0.1:8099 --worker-log /tmp/worker.log

Exits non-zero with a specific reason on the first failure; prints `OK` last.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "http://127.0.0.1:8000"
RECEIVER = "http://127.0.0.1:8099"
WORKER_LOG = "/tmp/worker.log"
TIMEOUT = 60.0

# Where the worker log had grown to when the run started. Only text appended
# after this point counts, because the worker prints every registered task name
# in its startup banner - so grepping the whole log for "support.notify" is
# satisfied the moment the worker boots, whether or not it ever ran anything.
_worker_log_offset = 0


def fail(reason: str, detail: str = "") -> None:
    print(f"FAIL: {reason}")
    if detail:
        print(detail)
    sys.exit(1)


def request(
    method: str,
    url: str,
    body: dict | None = None,
    token: str | None = None,
) -> tuple[int, object]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return exc.code, raw.decode("utf-8", "replace")


def wait_for(url: str, what: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            status, _ = request("GET", url)
            if status == 200:
                return
            last = f"status {status}"
        except Exception as exc:
            last = repr(exc)
        time.sleep(0.5)
    fail(f"{what} never became reachable at {url}", f"last: {last}")


def promote_to_admin(email: str) -> None:
    """Flip the role in PostgreSQL rather than going through an API.

    There is no bootstrap-admin path in the product, so the first admin is
    created by a human editing the database. Doing the same here keeps the test
    honest about how the system actually gets its first admin.
    """
    from sqlalchemy import text

    from app.core.database import engine

    with engine.begin() as connection:
        result = connection.execute(
            text("UPDATE users SET role = 'admin' WHERE email = :email"),
            {"email": email},
        )
    if result.rowcount != 1:
        fail(f"could not promote {email} to admin (rowcount={result.rowcount})")


def configure_as_producer() -> None:
    """Make this process a producer, not a worker.

    Everything in the project defaults `CELERY_TASK_ALWAYS_EAGER` to 1, which
    would run phase 2's task inline in this process and pass without a broker
    being involved at all - the exact thing this test exists to rule out. Must
    be called before `app.core.workers` is imported, because the flag is read at
    import time.
    """
    os.environ["CELERY_TASK_ALWAYS_EAGER"] = "0"
    os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/0")


def worker_received_task(offset: int) -> bool:
    """True only if the worker logged *receiving* the task after `offset`.

    Requires the worker to run at `--loglevel=debug`, which is what makes
    Celery's "Task support.notify[...] received" line appear; at INFO it logs
    nothing per task.
    """
    try:
        with open(WORKER_LOG, encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            log = handle.read()
    except OSError:
        return False
    return "support.notify" in log and "received" in log


def received_events() -> list[dict]:
    _, payload = request("GET", f"{RECEIVER}/seen")
    return payload if isinstance(payload, list) else []


def wait_for_webhook(ticket_id: str) -> dict:
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        for body in received_events():
            if body.get("event") == "ticket.created" and body.get("ticket_id") == ticket_id:
                return body
        time.sleep(0.5)
    fail(
        "webhook for the created ticket never arrived",
        f"receiver saw: {json.dumps(received_events(), indent=2)}",
    )


def phase_app_level() -> str:
    email = f"e2e-{int(time.time())}@example.com"
    password = "correct horse battery"

    status, _ = request("POST", f"{API}/auth/register", {"email": email, "password": password})
    if status != 201:
        fail(f"register returned {status}, expected 201")

    promote_to_admin(email)

    status, login = request("POST", f"{API}/auth/login", {"email": email, "password": password})
    if status != 200 or not isinstance(login, dict) or "access_token" not in login:
        fail(f"login returned {status}", json.dumps(login))
    token = login["access_token"]

    status, created = request(
        "POST",
        f"{API}/tickets",
        {"customer_id": email, "message": "I was charged twice for my subscription", "channel": "web"},
        token=token,
    )
    if status not in (200, 201) or not isinstance(created, dict) or "id" not in created:
        fail(f"ticket creation returned {status}", json.dumps(created))
    ticket_id = created["id"]

    # The row has to be in PostgreSQL, not just returned in a response.
    from sqlalchemy import text

    from app.core.database import engine

    with engine.connect() as connection:
        stored = connection.execute(
            text("SELECT channel, intent, status FROM tickets WHERE id = :id"),
            {"id": ticket_id},
        ).fetchone()
    if stored is None:
        fail(f"ticket {ticket_id} is not in PostgreSQL")

    body = wait_for_webhook(ticket_id)
    print(f"  webhook delivered: {json.dumps(body)}")
    return ticket_id


def phase_task_level() -> None:
    from app.core.workers import celery_app, dispatch_notification

    async_result = dispatch_notification.apply_async(
        args=("e2e.synthetic", "synthetic-1", {"probe": True}),
    )
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        if async_result.ready():
            break
        time.sleep(0.5)
    if not async_result.ready():
        fail("direct apply_async task never completed", f"state={async_result.state}")
    if async_result.result() is None:
        fail("task returned None; the broker ran it but it did no work")

    result = async_result.result()
    if result.get("status") != "sent":
        fail(f"task result was {result}, expected status 'sent'")

    backend = celery_app.backend
    stored = backend.get_task_meta(async_result.id)
    if stored.get("status") != "SUCCESS":
        fail(f"result backend did not record SUCCESS, got {stored.get('status')}")
    print(f"  task return value round-tripped through Redis: {json.dumps(result)}")


def main() -> None:
    global API, RECEIVER, WORKER_LOG, _worker_log_offset

    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default=API)
    parser.add_argument("--receiver", default=RECEIVER)
    parser.add_argument("--worker-log", default=WORKER_LOG)
    args = parser.parse_args()
    API, RECEIVER, WORKER_LOG = args.api, args.receiver, args.worker_log

    # This driver is a producer, not a worker: see configure_as_producer.
    configure_as_producer()

    wait_for(f"{API}/health", "API")
    wait_for(f"{RECEIVER}/health", "webhook receiver")

    try:
        _worker_log_offset = os.path.getsize(WORKER_LOG)
    except OSError:
        _worker_log_offset = 0

    print("phase 1: app-level request path through a real broker")
    phase_app_level()

    if not worker_received_task(_worker_log_offset):
        fail(
            "the worker never logged receiving support.notify, so the webhook "
            "cannot have come from the worker - either the API ran the task "
            "eagerly, or no worker is consuming the queue",
        )

    print("phase 2: task return value through a real result backend")
    phase_task_level()

    print("OK")


if __name__ == "__main__":
    main()
