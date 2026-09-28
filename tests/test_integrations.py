import pytest

from app.agents.integrations import (
    IntegrationError,
    MockHelpdeskProvider,
    ZendeskProvider,
    get_provider,
)


def test_mock_provider_creates_and_pushes_comments() -> None:
    provider = MockHelpdeskProvider()
    created = provider.create_or_update_ticket({"id": "t-9", "message": "Help me"})
    assert created["status"] == "synced"
    assert created["remote_id"] == "mock-t-9"

    comment = provider.push_comment("mock-t-9", {"id": "c-1", "body": "Resolved"})
    assert comment["status"] == "synced"
    assert comment["remote_comment_id"] == "mock-comment-c-1"


def test_get_provider_requires_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUPPORT_TOOL_PROVIDER", raising=False)
    with pytest.raises(IntegrationError, match="SUPPORT_TOOL_PROVIDER"):
        get_provider()


def test_get_provider_returns_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPPORT_TOOL_PROVIDER", "mock")
    assert get_provider().name == "mock"


def test_zendesk_provider_rejects_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZENDESK_SUBDOMAIN", raising=False)
    monkeypatch.delenv("ZENDESK_API_EMAIL", raising=False)
    monkeypatch.delenv("ZENDESK_API_TOKEN", raising=False)
    with pytest.raises(IntegrationError, match="ZENDESK_SUBDOMAIN"):
        ZendeskProvider().validate_config()


def test_zendesk_provider_creates_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "acme")
    monkeypatch.setenv("ZENDESK_API_EMAIL", "agent@acme.com")
    monkeypatch.setenv("ZENDESK_API_TOKEN", "secret")
    calls = []

    def fake_http(method: str, url: str, headers: dict, payload: dict | None):
        calls.append((method, url, headers, payload))
        return 201, {"ticket": {"id": 123}}

    provider = ZendeskProvider(http_request=fake_http)
    result = provider.create_or_update_ticket(
        {"id": "t-1", "message": "Please help", "subject": "Error", "priority": "normal"}
    )

    assert result["status"] == "synced"
    assert result["remote_id"] == "123"
    method, url, _headers, payload = calls[0]
    assert method == "POST"
    assert url == "https://acme.zendesk.com/api/v2/tickets.json"
    assert payload["ticket"]["external_id"] == "t-1"


def test_zendesk_provider_pushes_comment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "acme")
    monkeypatch.setenv("ZENDESK_API_EMAIL", "agent@acme.com")
    monkeypatch.setenv("ZENDESK_API_TOKEN", "secret")
    calls = []

    def fake_http(method: str, url: str, headers: dict, payload: dict | None):
        calls.append((method, url, payload))
        return 201, {"id": 456}

    provider = ZendeskProvider(http_request=fake_http)
    result = provider.push_comment("123", {"id": "c-2", "body": "All fixed", "is_internal": False})

    assert result["status"] == "synced"
    assert result["remote_comment_id"] == "456"
    assert calls[0][0] == "POST"
    assert calls[0][1] == "https://acme.zendesk.com/api/v2/tickets/123/comments.json"
    assert calls[0][2]["ticket"]["comment"]["public"] is True