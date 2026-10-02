"""Provider-agnostic LLM gateway.

Centralizes OpenAI / Azure OpenAI calls behind a single interface with
rate limiting (shared across replicas when `RATE_LIMIT_BACKEND=redis`),
retry-with-backoff, prompt/response logging, and token-based cost tracking. Provider selection is driven by the
``LLM_PROVIDER`` environment variable (``mock``, ``openai``, or
``azure_openai``) and is opt-in: when unset the gateway raises
``LLMNotConfigured`` so downstream agents can degrade gracefully.

``mock`` is a deterministic, credential-free provider for development and
tests, mirroring the ``mock`` helpdesk provider in ``app/agents/integrations.py``.
Real providers use raw JSON over HTTPS (no SDK dependency) and accept an
injected ``http_request`` transport for unit tests.
"""

import json
import os
import random
import re
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Callable, Generic, TypeVar

from pydantic import BaseModel, Field, ValidationError

from app.agents.guardrails import redact
from app.core.http_json import default_json_request
from app.core.observability import get_app_logger
from app.security import rate_limit


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


class LLMContractError(LLMError):
    """The provider answered, but not in the shape the prompt asked for.

    Distinct from `LLMRetryableError`: the HTTP call succeeded and the tokens
    were spent, so retrying the same request usually reproduces the same
    unusable payload. Callers decide what an unusable payload means for them
    (triage falls back to its keyword classifier, the draft agent forces
    review, agent assist reports the failure) - this class only guarantees the
    refusal is a typed, recorded event rather than a silent substitution.
    """

    def __init__(
        self,
        message: str,
        *,
        contract: str = "",
        provider: str = "",
        model: str = "",
    ) -> None:
        super().__init__(message)
        self.contract = contract
        self.provider = provider
        self.model = model


ModelT = TypeVar("ModelT", bound=BaseModel)


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


class _HttpProvider(LLMProvider):
    def __init__(self, http_request: Callable[..., Any] | None = None) -> None:
        self._http_request = http_request or default_json_request

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


_SOURCE_IN_EXCERPT = re.compile(r"Source (\d+) \(([^)]+)\):")

_MOCK_ASSIST_SUMMARY = (
    "The customer has reported an issue and is asking for help. A support "
    "specialist should review the ticket history below and confirm the next "
    "step with the customer."
)

_MOCK_GENERIC_REPLY = (
    "Thanks for reaching out. A support specialist is reviewing this and will "
    "follow up shortly."
)


def _mock_sources(system: str) -> list[tuple[str, str]]:
    """The `(label, source)` pairs rendered into a draft prompt's excerpts."""
    return [
        (match.group(1), match.group(2))
        for match in _SOURCE_IN_EXCERPT.finditer(system)
    ]


def _mock_draft_reply(system: str) -> str:
    """A canned reply that cites the excerpts it was given.

    Deliberately built from the prompt's own `Source N (file)` labels rather
    than from the customer message. The previous mock echoed the user's own
    text, and `run_auto_response` posts the draft to the customer as a public
    comment, so on the documented demo config the "AI answer" was the customer's
    message verbatim.
    """
    sources = _mock_sources(system)
    if not sources:
        return (
            "Thanks for getting in touch. I do not have a documented answer for "
            "this yet, so I have asked a specialist to review it and follow up "
            "with you shortly."
        )
    cited = " ".join(f"(Source {label})" for label, _ in sources[:2])
    titles = ", ".join(source for _, source in sources[:2])
    return (
        "Thanks for getting in touch, and sorry for the trouble. I checked our "
        f"documentation on {titles} {cited} and the steps there should resolve "
        "this. Please try them in order and reply with the result. If it still "
        "does not work, I will bring in a specialist who can take a closer look."
    )


def _mock_json_payload(system: str) -> dict[str, Any]:
    """A deterministic payload shaped like the contract the prompt asks for.

    Shape is inferred from the system prompt because the gateway passes the
    rendered template through as the system turn and the agents do not declare
    which template they used.
    """
    if "suggested_replies" in system:
        return {
            "summary": _MOCK_ASSIST_SUMMARY,
            "suggested_replies": [_MOCK_GENERIC_REPLY],
            "recommended_team": "",
        }
    if "citations" in system and "escalate" in system:
        sources = _mock_sources(system)
        return {
            "draft": _mock_draft_reply(system),
            "confidence": 0.9,
            "citations": [f"Source {label}" for label, _ in sources[:2]],
            "escalate": False,
            "reason": "The documented steps in the cited sources answer the question.",
        }
    # `triage.classify` is deliberately left unanswered. Triage falls back to
    # the deterministic keyword classifier, which produces a more realistic
    # intent than anything a canned payload could invent, so answering here
    # would replace a working classifier with a worse fake.
    return {"content": _MOCK_GENERIC_REPLY}


def _mock_text(system: str) -> str:
    """Plain-text completion.

    Returns empty for the escalation-summary prompt so `escalation.py` keeps its
    deterministic summary built from ticket fields. A non-empty mock string
    would suppress that fallback and leave the customer's own words sitting in
    `ticket.escalation_summary` as if it were a reviewer's briefing.
    """
    if "human reviewer" in system:
        return ""
    return _MOCK_GENERIC_REPLY


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
        system = _system_content(messages)
        if response_format == "json_object":
            text = json.dumps(_mock_json_payload(system), ensure_ascii=False)
        else:
            text = _mock_text(system)
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


def _system_content(messages: list[dict[str, str]]) -> str:
    for message in messages:
        if message.get("role") == "system":
            return str(message.get("content", ""))
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


LLM_RATE_LIMIT_WINDOW_SECONDS = 60


def _enforce_rate_limit(provider_name: str) -> None:
    """Cap calls per provider per minute.

    Shares `app.security.rate_limit` with the login and webhook throttles, so
    setting RATE_LIMIT_BACKEND=redis makes the cap cover every replica rather
    than granting each worker its own budget.
    """
    limit = int(os.getenv("LLM_RATE_LIMIT_PER_MINUTE", "60"))
    if limit <= 0:
        return
    key = f"llm_ratelimit:{provider_name}"
    if rate_limit.check(key, limit, LLM_RATE_LIMIT_WINDOW_SECONDS):
        raise LLMRateLimitExceeded(
            f"LLM rate limit exceeded for provider {provider_name!r}: "
            f"{limit} calls/minute"
        )
    rate_limit.record(key, limit, LLM_RATE_LIMIT_WINDOW_SECONDS)


def reset_rate_limits() -> None:
    rate_limit.reset_rate_limits()


def _backoff_seconds(attempt: int) -> float:
    return min(30.0, 1.0 * (2 ** attempt)) + random.uniform(0, 0.5)


_usages: list[dict[str, Any]] = []
_invalid_responses: list[dict[str, Any]] = []
_usage_lock = threading.Lock()

_invalid_response_sink: Callable[[dict[str, Any]], None] | None = None


def set_invalid_response_sink(sink: Callable[[dict[str, Any]], None] | None) -> None:
    """Redirect where unusable model output is recorded (tests use this).

    The default sink writes an audit row through `SessionLocal`, which in a
    test process points at the developer's `support.db` rather than the
    overridden test session, so tests inject an in-memory recorder instead.
    """
    global _invalid_response_sink
    _invalid_response_sink = sink


def record_invalid_response(record: dict[str, Any]) -> None:
    """Count an unusable model payload and persist it as `llm.invalid_response`.

    A completion that cannot be parsed used to be indistinguishable from a
    completion nobody read: `LLMUsage` counted it as a success and no audit
    row existed, so a provider silently drifting away from its prompt was
    invisible until someone noticed the quality of the answers. Best-effort
    like the notification dead letter - recording a bad response must never
    take down the request that was already going to degrade.
    """
    with _usage_lock:
        _invalid_responses.append(record)
    sink = _invalid_response_sink or _write_invalid_response_audit
    try:
        sink(record)
    except Exception:
        get_app_logger().exception(
            "Failed to record an invalid LLM response: %s", record.get("contract")
        )


def _write_invalid_response_audit(record: dict[str, Any]) -> None:
    from app.api.schemas import add_audit_log
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        add_audit_log(
            db,
            None,
            "llm.invalid_response",
            "llm",
            str(record.get("contract") or "unknown"),
            record,
        )
        db.commit()


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


def get_invalid_responses() -> list[dict[str, Any]]:
    with _usage_lock:
        return list(_invalid_responses)


def get_llm_usage_summary() -> dict[str, Any]:
    with _usage_lock:
        invalid_by_model: dict[str, int] = {}
        for entry in _invalid_responses:
            model = str(entry.get("model") or "unknown")
            invalid_by_model[model] = invalid_by_model.get(model, 0) + 1
        invalid_total = len(_invalid_responses)
        if not _usages:
            return {
                "total_calls": 0,
                "total_prompt_tokens": 0,
                "total_completion_tokens": 0,
                "total_cost_usd": 0.0,
                "since": None,
                "invalid_responses": invalid_total,
                "by_model": [],
            }
        per_model: dict[str, list[float]] = {}
        for entry in _usages:
            bucket = per_model.setdefault(entry["model"], [0.0, 0.0, 0.0, 0.0])
            bucket[0] += 1
            bucket[1] += entry["prompt_tokens"]
            bucket[2] += entry["completion_tokens"]
            bucket[3] += entry["cost_usd"]
        models = sorted(set(per_model) | set(invalid_by_model))
        return {
            "total_calls": len(_usages),
            "total_prompt_tokens": sum(entry["prompt_tokens"] for entry in _usages),
            "total_completion_tokens": sum(entry["completion_tokens"] for entry in _usages),
            "total_cost_usd": round(
                sum(entry["cost_usd"] for entry in _usages), 6
            ),
            "since": _usages[0]["ts"],
            "invalid_responses": invalid_total,
            "by_model": [
                {
                    "model": model,
                    "calls": int(per_model[model][0]) if model in per_model else 0,
                    "prompt_tokens": int(per_model[model][1]) if model in per_model else 0,
                    "completion_tokens": int(per_model[model][2]) if model in per_model else 0,
                    "cost_usd": round(per_model[model][3], 6) if model in per_model else 0.0,
                    "invalid_responses": invalid_by_model.get(model, 0),
                }
                for model in models
            ],
        }


def reset_llm_usage() -> None:
    with _usage_lock:
        _usages.clear()
        _invalid_responses.clear()


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


_JSON_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)
_INVALID_SAMPLE_CHARS = 500


def _strip_json_fence(text: str) -> str:
    """Unwrap a fenced code block.

    `response_format={"type": "json_object"}` is a request, not a guarantee -
    Azure OpenAI deployments in particular still answer ```json ... ``` often
    enough that refusing to unwrap would fail the whole contract over
    whitespace. Only a full-string fence is unwrapped, so a reply that merely
    mentions code fences inside a JSON string value is untouched.
    """
    match = _JSON_FENCE.match((text or "").strip())
    return match.group(1) if match else (text or "").strip()


def _reject_payload(
    result: LLMResult,
    *,
    contract: str,
    reason: str,
    detail: str,
) -> LLMContractError:
    """Record an unusable payload and build the typed refusal for the caller."""
    record_invalid_response(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "contract": contract,
            "provider": result.provider,
            "model": result.model,
            "reason": reason,
            "detail": detail[:_INVALID_SAMPLE_CHARS],
            "finish_reason": result.finish_reason,
            "prompt_tokens": result.usage.prompt_tokens,
            "completion_tokens": result.usage.completion_tokens,
            # Redacted: this text came from a provider that was just handed a
            # ticket, so it can contain customer PII and the audit log is
            # retained for a year.
            "response_sample": redact(result.text)[:_INVALID_SAMPLE_CHARS],
        }
    )
    return LLMContractError(
        f"provider {result.provider!r} broke the {contract!r} contract: {reason}: {detail}",
        contract=contract,
        provider=result.provider,
        model=result.model,
    )


def parse_json_object(
    result: LLMResult,
    *,
    contract: str,
) -> dict[str, Any]:
    """Decode a `json_object` completion, refusing anything else.

    The single place a provider reply becomes a Python object. Every caller
    used to do this itself, which is how three different failure policies grew
    out of one code path; the refusal is now identical and recorded.
    """
    raw = _strip_json_fence(result.text)
    if not raw:
        raise _reject_payload(result, contract=contract, reason="empty_response", detail="no text returned")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _reject_payload(
            result, contract=contract, reason="unparseable_json", detail=str(exc)
        ) from exc
    if not isinstance(parsed, dict):
        raise _reject_payload(
            result,
            contract=contract,
            reason="not_an_object",
            detail=f"got {type(parsed).__name__}",
        )
    return parsed


class StructuredCompletion(BaseModel, Generic[ModelT]):
    """A validated payload plus which provider/model produced it.

    The identity travels with the data because every caller needs it: the
    draft agent stamps it on the review record, agent assist shows it in the
    panel header, and triage ignores it. Recovering it from a module global
    after the fact would be wrong as soon as two calls overlap.
    """

    data: ModelT
    provider: str
    model: str


def generate_structured(
    provider: LLMProvider | str | None = None,
    prompt: str | None = None,
    *,
    system: str | None = None,
    max_tokens: int = 600,
    contract: type[ModelT],
    contract_name: str | None = None,
    temperature: float = 0.0,
) -> StructuredCompletion[ModelT]:
    """Run a `json_object` completion and validate it against `contract`.

    `contract` is a pydantic model whose field types *are* the prompt
    contract: required fields stay required, enums reject values outside the
    documented set, and ranges are enforced. `contract_name` labels the audit
    row and defaults to the model's class name.

    Raises `LLMContractError` - a typed, recorded refusal - rather than
    returning a half-populated object for the caller to silently substitute.
    What an unusable payload means is the caller's call: triage falls back to
    its keyword classifier, the draft agent forces human review, agent assist
    reports the failure to the agent.
    """
    name = contract_name or contract.__name__
    result = generate(
        provider,
        prompt,
        system=system,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format="json_object",
    )
    parsed = parse_json_object(result, contract=name)
    try:
        data = contract.model_validate(parsed)
    except ValidationError as exc:
        raise _reject_payload(
            result, contract=name, reason="schema_violation", detail=str(exc)
        ) from exc
    return StructuredCompletion(data=data, provider=result.provider, model=result.model)


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
    return parse_json_object(result, contract="generate_json")