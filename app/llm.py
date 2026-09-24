"""Provider-agnostic LLM gateway.

Centralizes OpenAI / Azure OpenAI calls behind a single interface with
process-local rate limiting, retry-with-backoff, prompt/response logging,
and token-based cost tracking. Provider selection is driven by the
``LLM_PROVIDER`` environment variable (``mock``, ``openai``, or
``azure_openai``) and is opt-in: when unset the gateway raises
``LLMNotConfigured`` so downstream agents can degrade gracefully.

``mock`` is a deterministic, credential-free provider for development and
tests, mirroring the ``mock`` helpdesk provider in ``app/integrations.py``.
Real providers use raw JSON over HTTPS (no SDK dependency) and accept an
injected ``http_request`` transport for unit tests.
"""

import json
import os
import random
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.request import Request, urlopen

from pydantic import BaseModel, Field

from app.guardrails import redact
from app.observability import get_app_logger


class LLMError(Exception):
    """Base error for the LLM gateway."""


class LLMConfigError(LLMError):
    """Raised when a provider is misconfigured. Not retried."""


class LLMNotConfigured(LLMConfigError):
    """Raised when no LLM provider is selected."""


class LLMRateLimitExceeded(LLMError):
    """Raised when the process-local rate limit is exceeded."""


class LLMRetryableError(LLMError):
    """Raised for transient failures (429, 5xx, network). Retried."""


class LLMUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class LLMResult(BaseModel):
    text: str
    model: str
    provider: str
    finish_reason: str = "stop"
    usage: LLMUsage = Field(default_factory=LLMUsage)
    estimated_cost_usd: float = 0.0
    latency_ms: float = 0.0


_MODEL_PRICING_PER_1M: list[tuple[str, float, float]] = [
    ("gpt-4o-mini", 0.15, 0.60),
    ("gpt-4o", 2.50, 10.00),
    ("gpt-4.1-mini", 0.40, 1.60),
    ("gpt-4.1", 2.00, 8.00),
    ("gpt-4", 30.00, 60.00),
    ("gpt-3.5-turbo", 0.50, 1.50),
]


def _estimate_cost_usd(model: str, usage: LLMUsage) -> float:
    normalized = model.lower()
    for prefix, input_price, output_price in _MODEL_PRICING_PER_1M:
        if normalized.startswith(prefix):
            return (
                usage.prompt_tokens * input_price
                + usage.completion_tokens * output_price
            ) / 1_000_000
    return 0.0


class LLMProvider(ABC):
    name = "llm"

    def validate_config(self) -> None:
        raise LLMConfigError(f"{self.name} provider is not configured")

    @abstractmethod
    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 600,
        response_format: str | None = None,
    ) -> LLMResult:
        """Run a chat completion and return the annotated result."""


def _default_http_request(
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None,
) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(url, data=body, headers=headers, method=method)
    with urlopen(request, timeout=30) as response:
        raw = response.read()
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
        return response.status, parsed


class _HttpProvider(LLMProvider):
    def __init__(self, http_request: Callable[..., Any] | None = None) -> None:
        self._http_request = http_request or _default_http_request

    def _respond(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        model: str,
    ) -> LLMResult:
        try:
            status, body = self._http_request("POST", url, headers, payload)
        except Exception as exc:
            raise LLMRetryableError(f"{self.name} transport failure: {exc}") from exc
        description = None
        if isinstance(body, dict):
            error_payload = body.get("error")
            description = (
                error_payload.get("message") if isinstance(error_payload, dict) else None
            )
        if status in {429} or status >= 500:
            raise LLMRetryableError(
                f"{self.name} API status {status}: {description or body}"
            )
        if status >= 400:
            raise LLMError(f"{self.name} API error status {status}: {description or body}")
        choice = body["choices"][0]
        message = choice["message"]
        usage_raw = body.get("usage") or {}
        usage = LLMUsage(
            prompt_tokens=int(usage_raw.get("prompt_tokens", 0)),
            completion_tokens=int(usage_raw.get("completion_tokens", 0)),
            total_tokens=int(usage_raw.get("total_tokens", 0)),
        )
        return LLMResult(
            text=message.get("content") or "",
            model=model,
            provider=self.name,
            finish_reason=choice.get("finish_reason", "stop"),
            usage=usage,
            estimated_cost_usd=_estimate_cost_usd(model, usage),
        )


class OpenAIProvider(_HttpProvider):
    name = "openai"

    def validate_config(self) -> None:
        if not os.getenv("OPENAI_API_KEY"):
            raise LLMConfigError("OpenAI provider requires OPENAI_API_KEY")

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 600,
        response_format: str | None = None,
    ) -> LLMResult:
        base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
        headers = {
            "Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            payload["response_format"] = {"type": response_format}
        return self._respond(f"{base_url}/chat/completions", headers, payload, model)


class AzureOpenAIProvider(_HttpProvider):
    name = "azure_openai"

    def validate_config(self) -> None:
        missing = [
            variable
            for variable in (
                "AZURE_OPENAI_ENDPOINT",
                "AZURE_OPENAI_DEPLOYMENT",
                "AZURE_OPENAI_API_KEY",
            )
            if not os.getenv(variable)
        ]
        if missing:
            required = ", ".join(missing)
            raise LLMConfigError(f"Azure OpenAI provider requires {required}")

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 600,
        response_format: str | None = None,
    ) -> LLMResult:
        endpoint = os.getenv("AZURE_OPENAI_ENDPOINT").rstrip("/")
        deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT")
        api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
        url = (
            f"{endpoint}/openai/deployments/{deployment}/chat/completions"
            f"?api-version={api_version}"
        )
        headers = {
            "api-key": os.getenv("AZURE_OPENAI_API_KEY"),
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            payload["response_format"] = {"type": response_format}
        return self._respond(url, headers, payload, deployment)


def _tokenish_word_count(text: str) -> int:
    return len(text.split())


class MockLLMProvider(LLMProvider):
    name = "mock"

    def validate_config(self) -> None:
        return None

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 600,
        response_format: str | None = None,
    ) -> LLMResult:
        content = _last_content(messages)
        if response_format == "json_object":
            text = json.dumps(
                {
                    "content": content,
                    "provider": "mock",
                    "model": "mock-llm",
                    "confidence": 0.9,
                },
                ensure_ascii=False,
            )
        else:
            text = content if content else "Mock reply"
        usage = LLMUsage(
            prompt_tokens=sum(_tokenish_word_count(m.get("content", "")) for m in messages) + 4,
            completion_tokens=_tokenish_word_count(text),
            total_tokens=sum(_tokenish_word_count(m.get("content", "")) for m in messages)
            + 4
            + _tokenish_word_count(text),
        )
        return LLMResult(
            text=text,
            model="mock-llm",
            provider=self.name,
            finish_reason="stop",
            usage=usage,
            estimated_cost_usd=_estimate_cost_usd("mock-llm", usage),
        )


def _last_content(messages: list[dict[str, str]]) -> str:
    for message in reversed(messages):
        role = message.get("role")
        content = str(message.get("content", ""))
        if role in {"user", "assistant"} and content:
            return content
    return ""


PROVIDERS: dict[str, type[LLMProvider]] = {
    "mock": MockLLMProvider,
    "openai": OpenAIProvider,
    "azure_openai": AzureOpenAIProvider,
}


def get_provider(name: str | None = None) -> LLMProvider:
    provider_name = (name or os.getenv("LLM_PROVIDER", "")).strip().lower()
    provider_class = PROVIDERS.get(provider_name)
    if provider_class is None:
        raise LLMNotConfigured("No LLM provider configured (set LLM_PROVIDER)")
    provider = provider_class()
    provider.validate_config()
    return provider


_rate_buckets: dict[str, list[float]] = {}
_rate_lock = threading.Lock()


def _enforce_rate_limit(provider_name: str) -> None:
    limit = int(os.getenv("LLM_RATE_LIMIT_PER_MINUTE", "60"))
    if limit <= 0:
        return
    now = time.monotonic()
    window_start = now - 60
    with _rate_lock:
        recent = [
            timestamp
            for timestamp in _rate_buckets.get(provider_name, [])
            if timestamp > window_start
        ]
        if len(recent) >= limit:
            raise LLMRateLimitExceeded(
                f"LLM rate limit exceeded for provider {provider_name!r}: "
                f"{limit} calls/minute"
            )
        recent.append(now)
        _rate_buckets[provider_name] = recent


def reset_rate_limits() -> None:
    with _rate_lock:
        _rate_buckets.clear()


def _backoff_seconds(attempt: int) -> float:
    return min(30.0, 1.0 * (2 ** attempt)) + random.uniform(0, 0.5)


_usages: list[dict[str, Any]] = []
_usage_lock = threading.Lock()


def _record_usage(result: LLMResult) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "provider": result.provider,
        "model": result.model,
        "prompt_tokens": result.usage.prompt_tokens,
        "completion_tokens": result.usage.completion_tokens,
        "total_tokens": result.usage.total_tokens,
        "cost_usd": round(result.estimated_cost_usd, 6),
    }
    with _usage_lock:
        _usages.append(entry)


def get_llm_usage_summary() -> dict[str, Any]:
    with _usage_lock:
        if not _usages:
            return {
                "total_calls": 0,
                "total_prompt_tokens": 0,
                "total_completion_tokens": 0,
                "total_cost_usd": 0.0,
                "since": None,
                "by_model": [],
            }
        per_model: dict[str, list[float]] = {}
        for entry in _usages:
            bucket = per_model.setdefault(entry["model"], [0.0, 0.0, 0.0, 0.0])
            bucket[0] += 1
            bucket[1] += entry["prompt_tokens"]
            bucket[2] += entry["completion_tokens"]
            bucket[3] += entry["cost_usd"]
        return {
            "total_calls": len(_usages),
            "total_prompt_tokens": sum(entry["prompt_tokens"] for entry in _usages),
            "total_completion_tokens": sum(entry["completion_tokens"] for entry in _usages),
            "total_cost_usd": round(
                sum(entry["cost_usd"] for entry in _usages), 6
            ),
            "since": _usages[0]["ts"],
            "by_model": [
                {
                    "model": model,
                    "calls": int(bucket[0]),
                    "prompt_tokens": int(bucket[1]),
                    "completion_tokens": int(bucket[2]),
                    "cost_usd": round(bucket[3], 6),
                }
                for model, bucket in sorted(per_model.items())
            ],
        }


def reset_llm_usage() -> None:
    with _usage_lock:
        _usages.clear()


def _log_call(
    provider_name: str,
    messages: list[dict[str, str]],
    result: LLMResult,
) -> None:
    get_app_logger().info(
        "llm.complete",
        extra={
            "provider": provider_name,
            "model": result.model,
            "prompt_tokens": result.usage.prompt_tokens,
            "completion_tokens": result.usage.completion_tokens,
            "cost_usd": round(result.estimated_cost_usd, 6),
            "latency_ms": round(result.latency_ms, 2),
            "prompt": redact("\n".join(m.get("content", "") for m in messages)),
            "response": redact(result.text),
        },
    )


def _resolve_provider(provider: LLMProvider | str | None) -> LLMProvider:
    if provider is None:
        return get_provider()
    if isinstance(provider, LLMProvider):
        return provider
    return get_provider(str(provider))


def chat(
    provider: LLMProvider | str | None = None,
    messages: list[dict[str, str]] | None = None,
    *,
    temperature: float = 0.2,
    max_tokens: int = 600,
    response_format: str | None = None,
) -> LLMResult:
    if not messages:
        raise ValueError("messages must be a non-empty list")
    instance = _resolve_provider(provider)
    _enforce_rate_limit(instance.name)
    max_attempts = 1 + max(0, int(os.getenv("LLM_MAX_RETRIES", "3")))
    last_error: LLMError | None = None
    for attempt in range(max_attempts):
        try:
            started = time.perf_counter()
            result = instance.complete(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
            )
        except LLMRetryableError as exc:
            last_error = exc
            if attempt + 1 >= max_attempts:
                break
            time.sleep(_backoff_seconds(attempt))
            continue
        result.latency_ms = (time.perf_counter() - started) * 1000
        _record_usage(result)
        _log_call(instance.name, messages, result)
        return result
    raise LLMError(f"LLM call failed after {max_attempts} attempt(s): {last_error}") from last_error


def generate(
    provider: LLMProvider | str | None = None,
    prompt: str | None = None,
    *,
    system: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 600,
    response_format: str | None = None,
) -> LLMResult:
    if not prompt:
        raise ValueError("prompt must be a non-empty string")
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return chat(
        provider,
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format=response_format,
    )


def generate_json(
    provider: LLMProvider | str | None = None,
    prompt: str | None = None,
    *,
    system: str | None = None,
    max_tokens: int = 600,
) -> dict[str, Any]:
    result = generate(
        provider,
        prompt,
        system=system,
        temperature=0.0,
        max_tokens=max_tokens,
        response_format="json_object",
    )
    try:
        parsed = json.loads(result.text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"provider {result.provider!r} returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise LLMError(f"provider {result.provider!r} returned non-object JSON")
    return parsed