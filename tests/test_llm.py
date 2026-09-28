import json

import pytest

from app.agents import llm, prompts


@pytest.fixture(autouse=True)
def clean_gateway_state():
    llm.reset_llm_usage()
    llm.reset_rate_limits()
    yield
    llm.reset_llm_usage()
    llm.reset_rate_limits()


def test_mock_provider_returns_deterministic_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    result = llm.chat(
        messages=[
            {"role": "system", "content": "Be helpful."},
            {"role": "user", "content": "hello there"},
        ]
    )
    assert result.text == "hello there"
    assert result.provider == "mock"
    assert result.model == "mock-llm"
    assert result.finish_reason == "stop"
    assert result.usage.total_tokens > 0


def test_mock_provider_returns_structurable_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    result = llm.chat(
        messages=[{"role": "user", "content": "summarize this"}],
        response_format="json_object",
    )
    parsed = json.loads(result.text)
    assert parsed["content"] == "summarize this"
    assert parsed["provider"] == "mock"
    assert parsed["confidence"] == 0.9


def test_get_provider_requires_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    with pytest.raises(llm.LLMNotConfigured, match="LLM_PROVIDER"):
        llm.get_provider()


def test_chat_requires_messages() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        llm.chat(messages=[])


def test_openai_provider_validates_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(llm.LLMConfigError, match="OPENAI_API_KEY"):
        llm.OpenAIProvider().validate_config()


def test_azure_provider_validates_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_DEPLOYMENT", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    with pytest.raises(llm.LLMConfigError, match="AZURE_OPENAI_ENDPOINT"):
        llm.AzureOpenAIProvider().validate_config()


def test_openai_provider_builds_request_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")
    captured: dict = {}

    def fake_http_request(method, url, headers, payload):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = payload
        return (
            200,
            {
                "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    provider = llm.OpenAIProvider(http_request=fake_http_request)
    provider.validate_config()
    result = provider.complete(
        [{"role": "user", "content": "hello"}], response_format="json_object"
    )

    assert result.text == "hi"
    assert result.usage.total_tokens == 15
    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["payload"]["model"] == "gpt-4o-mini"
    assert captured["payload"]["messages"] == [{"role": "user", "content": "hello"}]
    assert captured["payload"]["response_format"] == {"type": "json_object"}


def test_azure_provider_builds_deployment_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-deploy")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-key")
    captured: dict = {}

    def fake_http_request(method, url, headers, payload):
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = payload
        return (
            200,
            {"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 3}},
        )

    provider = llm.AzureOpenAIProvider(http_request=fake_http_request)
    provider.validate_config()
    result = provider.complete([{"role": "user", "content": "bonjour"}])

    assert result.text == "ok"
    assert "deployments/gpt-deploy/chat/completions" in captured["url"]
    assert "api-version=2024-10-21" in captured["url"]
    assert captured["headers"]["api-key"] == "azure-key"
    assert captured["payload"]["messages"] == [{"role": "user", "content": "bonjour"}]
    assert "model" not in captured["payload"]


def test_chat_retries_transient_failures_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(llm.time, "sleep", lambda _seconds: None)
    calls = {"count": 0}

    def fake_http_request(method, url, headers, payload):
        calls["count"] += 1
        if calls["count"] < 3:
            return 503, {"error": {"message": "unavailable"}}
        return (
            200,
            {"choices": [{"message": {"content": "finally"}}], "usage": {"total_tokens": 5}},
        )

    provider = llm.OpenAIProvider(http_request=fake_http_request)
    result = llm.chat(provider, [{"role": "user", "content": "hello"}])

    assert result.text == "finally"
    assert calls["count"] == 3


def test_chat_raises_after_retries_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_MAX_RETRIES", "1")
    monkeypatch.setattr(llm.time, "sleep", lambda _seconds: None)

    def fake_http_request(method, url, headers, payload):
        return 503, {"error": {"message": "down"}}

    provider = llm.OpenAIProvider(http_request=fake_http_request)
    with pytest.raises(llm.LLMError, match="after 2 attempt"):
        llm.chat(provider, [{"role": "user", "content": "hello"}])


def test_non_retryable_http_error_is_surfaced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    def fake_http_request(method, url, headers, payload):
        return 400, {"error": {"message": "bad request"}}

    provider = llm.OpenAIProvider(http_request=fake_http_request)
    with pytest.raises(llm.LLMError, match="400"):
        llm.chat(provider, [{"role": "user", "content": "hello"}])


def test_rate_limit_blocks_once_budget_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("LLM_RATE_LIMIT_PER_MINUTE", "2")

    llm.generate(prompt="first")
    llm.generate(prompt="second")

    with pytest.raises(llm.LLMRateLimitExceeded, match="rate limit"):
        llm.generate(prompt="third")


def test_usage_is_tracked_and_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "mock")

    llm.generate(prompt="hello world", system="greet")
    summary = llm.get_llm_usage_summary()

    assert summary["total_calls"] == 1
    assert summary["total_prompt_tokens"] > 0
    assert summary["total_completion_tokens"] > 0
    assert summary["since"] is not None
    assert summary["by_model"][0]["model"] == "mock-llm"
    assert summary["by_model"][0]["calls"] == 1

    llm.reset_llm_usage()
    assert llm.get_llm_usage_summary()["total_calls"] == 0


def test_generate_json_returns_parsed_object(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    parsed = llm.generate_json(prompt="Refund please", system="You are triage.")
    assert parsed["content"] == "Refund please"
    assert parsed["provider"] == "mock"


def test_generate_json_raises_on_invalid_object() -> None:
    class BadProvider(llm.MockLLMProvider):
        def complete(self, messages, **kwargs):
            return llm.LLMResult(text="not json", model="bad", provider="mock")

    with pytest.raises(llm.LLMError, match="invalid JSON"):
        llm.generate_json(BadProvider(), prompt="hi")


def test_template_renders_triage_prompt() -> None:
    template = prompts.get_template("triage.classify")
    rendered = template.render(message="I was charged twice")
    assert template.version == 1
    assert "I was charged twice" in rendered
    assert "fraud" in rendered


def test_template_rejects_missing_and_unexpected_params() -> None:
    template = prompts.get_template("response.draft")
    with pytest.raises(prompts.PromptTemplateError, match="missing"):
        template.render(message="hi")
    with pytest.raises(prompts.PromptTemplateError, match="unexpected"):
        template.render(message="hi", excerpts="docs", bogus="x")


def test_get_template_unknown() -> None:
    with pytest.raises(prompts.PromptTemplateError, match="unknown template"):
        prompts.get_template("nope")


def test_all_registered_templates_render_required_params() -> None:
    sample = {
        "triage.classify": {"message": "help"},
        "response.draft": {"excerpts": "docs", "message": "help"},
        "response.draft_json": {"excerpts": "docs"},
        "escalation.summary": {"details": "details here"},
        "agent_assist.suggest": {"history": "history here"},
        "agent_assist.suggest_json": {"history": "history here"},
    }
    for name, template in prompts.TEMPLATES.items():
        assert name in sample
        rendered = template.render(**sample[name])
        assert rendered.strip()