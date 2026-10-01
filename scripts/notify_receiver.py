"""A stand-in for the outbound notification webhook.

`support.notify` does exactly one thing: POST a signed JSON body to
`NOTIFY_WEBHOOK_URL`. There was no way to observe that happening end to end
before, because every test monkeypatched `enqueue_notification` and so never
exercised the real `.delay()` path.

Used by the CI job that runs a non-eager worker against a real Redis broker, and
runnable by hand. Pass the expected signing secret as the second argument (or
`NOTIFY_WEBHOOK_SECRET`); when one is set every POST is signature-verified and
an unsigned or tampered body is answered 401 and not recorded, so the CI job
fails if signing regresses. It records what it receives in memory and answers:

  POST /hook   -> 200 when the signature verifies (or none was expected),
                 401 otherwise; records the body and its headers
  GET  /seen   -> the list of received bodies as JSON, `[]` when empty
  GET  /health -> 200

Poll `/seen` rather than sleeping a fixed interval: a fixed sleep either wastes
time on success or flakes on a slow runner.
"""

import hmac
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.workers import sign_notification_body  # noqa: E402

RECEIVED: list[dict] = []
REJECTED: list[dict] = []
SECRET = ""
MAX_SKEW_SECONDS = 300


def verify(raw: bytes, header_value: str | None, secret: str) -> tuple[bool, str]:
    """Recompute the signature over the received bytes and check its freshness."""
    if not header_value:
        return False, "missing X-Support-Signature"
    parts = dict(
        piece.split("=", 1) for piece in header_value.split(",") if "=" in piece
    )
    timestamp = parts.get("t", "")
    provided = parts.get("v1", "")
    if not timestamp or not provided:
        return False, "malformed X-Support-Signature"
    try:
        age = abs(time.time() - int(timestamp))
    except ValueError:
        return False, "non-integer timestamp"
    if age > MAX_SKEW_SECONDS:
        return False, f"timestamp too old ({age:.0f}s)"
    expected = sign_notification_body(raw, secret, timestamp)
    if not hmac.compare_digest(expected, provided):
        return False, "signature mismatch"
    return True, "ok"


class Handler(BaseHTTPRequestHandler):
    def _respond(self, status: int, body: bytes = b"") -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"_unparseable": raw.decode("utf-8", "replace")}

        if SECRET:
            ok, reason = verify(raw, self.headers.get("X-Support-Signature"), SECRET)
            if not ok:
                REJECTED.append({"reason": reason, "body": payload})
                print(f"REJECTED {reason}: {json.dumps(payload)}", flush=True)
                self._respond(401, b'{"status":"invalid signature"}')
                return

        RECEIVED.append(payload)
        print(f"received {json.dumps(payload)}", flush=True)
        self._respond(200, b'{"status":"received"}')

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        if self.path == "/health":
            self._respond(200, b'{"status":"ok"}')
            return
        if self.path == "/rejected":
            self._respond(200, json.dumps(REJECTED).encode("utf-8"))
            return
        body = json.dumps(RECEIVED).encode("utf-8")
        self._respond(200, body)

    def log_message(self, fmt: str, *args: object) -> None:
        # stderr by default; CI captures it and it is pure noise next to the
        # one line per received body that is printed in do_POST.
        pass


def main() -> None:
    global SECRET
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    SECRET = (
        sys.argv[2] if len(sys.argv) > 2 else os.getenv("NOTIFY_WEBHOOK_SECRET", "")
    )
    print(f"listening on {port}, verifying={bool(SECRET)}", flush=True)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
