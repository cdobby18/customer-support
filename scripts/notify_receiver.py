"""A stand-in for the outbound notification webhook.

`support.notify` does exactly one thing: POST a JSON body to
`NOTIFY_WEBHOOK_URL`. There was no way to observe that happening end to end
before, because every test monkeypatched `enqueue_notification` and so never
exercised the real `.delay()` path.

Used by the CI job that runs a non-eager worker against a real Redis broker, and
runnable by hand. It records what it receives in memory and answers:

  POST /hook   -> 200, records the body
  GET  /seen   -> the list of received bodies as JSON, `[]` when empty
  GET  /health -> 200

Poll `/seen` rather than sleeping a fixed interval: a fixed sleep either wastes
time on success or flakes on a slow runner.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

RECEIVED: list[dict] = []


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
        RECEIVED.append(payload)
        print(f"received {json.dumps(payload)}", flush=True)
        self._respond(200, b'{"status":"received"}')

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        if self.path == "/health":
            self._respond(200, b'{"status":"ok"}')
            return
        body = json.dumps(RECEIVED).encode("utf-8")
        self._respond(200, body)

    def log_message(self, fmt: str, *args: object) -> None:
        # stderr by default; CI captures it and it is pure noise next to the
        # one line per received body that is printed in do_POST.
        pass


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
