import json
import logging

from app.observability import JsonFormatter, _request_id_ctx


def test_request_id_default_is_none() -> None:
    assert _request_id_ctx.get() is None


def test_json_formatter_produces_parseable_json() -> None:
    record = logging.LogRecord("app", logging.INFO, __file__, 1, "hello", (), None)
    record.method = "GET"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["message"] == "hello"
    assert payload["method"] == "GET"
    assert payload["level"] == "INFO"


def test_request_id_embedded_in_formatted_log() -> None:
    token = _request_id_ctx.set("embedded-req-99")
    try:
        record = logging.LogRecord("app", logging.INFO, __file__, 1, "x", (), None)
        payload = json.loads(JsonFormatter().format(record))
        assert payload["request_id"] == "embedded-req-99"
    finally:
        _request_id_ctx.reset(token)