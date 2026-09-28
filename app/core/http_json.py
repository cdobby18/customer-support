"""Minimal JSON-over-HTTP transport shared by the LLM and embedding clients.

Both clients need the same "POST/GET a JSON body, parse a JSON response"
behaviour and both accept an injected transport for tests, so the default
implementation lives here once instead of being copied per client.
"""

import json
from typing import Any
from urllib.request import Request, urlopen

REQUEST_TIMEOUT_SECONDS = 30


def default_json_request(
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None,
) -> tuple[int, dict[str, Any]]:
    """Send a JSON request and return ``(status_code, parsed_body)``."""
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(url, data=body, headers=headers, method=method)
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        raw = response.read()
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
        return response.status, parsed
