import json

import pytest

from app.agents import llm, response_agent
from app.agents.knowledge import KnowledgeMatch


def kb_matches() -> list[KnowledgeMatch]:
    return [
        KnowledgeMatch(
            document_id="account-access",
            title="Account access",
            source="account-access.md",
            excerpt="Use the password reset flow first.",
            score=0.91,
        ),
        KnowledgeMatch(
            document_id="technical-troubleshooting",
            title="Technical troubleshooting",
            source="technical-troubleshooting.md",
            excerpt="Ask for the visible error message.",
            score=0.62,
        ),
    ]


class FakeLLMProvider(llm.LLMProvider):
    name = "fake"

    def __init__(self, payload: dict | None = None, *, text: str | None = None):
        self._text = text if text is not None else json.dumps(payload or {})

    def validate_config(self) -> None:
        return None

    def complete(self, messages, *, temperature=0.2, max_tokens=600, response_format=None):
        return llm.LLMResult(text=self._text, model="fake-model", provider=self.name, usage=llm.LLMUsage(total_tokens=1))


@pytest.fixture(autouse=True)
def isolate_kb(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(response_agent, "search_knowledge", lambda message, limit=5: kb_matches())


def test_confident_draft_is_ready_to_send() -> None:
    payload = {
        "draft": "Please reset your password via the reset flow.",
        "confidence": 0.9,
        "citations": ["Source 1"],
        "escalate": False,
        "reason": "Account access issue.",
    }
    result = response_agent.draft_reply(
        "I am locked out of my account",
        provider=FakeLLMProvider(payload),
    )

    assert result.needs_review is False
    assert result.reasons == ["auto_reply_ready"]
    assert result.draft == payload["draft"]
    assert result.confidence == 0.9
    assert [citation.document_id for citation in result.citations] == ["account-access"]
    assert result.provider == "fake"
    assert result.model == "fake-model"
    assert result.guardrail_violations == []
    assert result.template_version == 1


def test_low_confidence_draft_requires_review() -> None:
    payload = {
        "draft": "Please reset your password via the reset flow.",
        "confidence": 0.6,
        "citations": ["Source 1"],
        "escalate": False,
    }
    result = response_agent.draft_reply(
        "I am locked out",
        provider=FakeLLMProvider(payload),
    )

    assert result.needs_review is True
    assert "low_confidence" in result.reasons


def test_threshold_can_be_overridden() -> None:
    payload = {
        "draft": "Please reset your password via the reset flow.",
        "confidence": 0.6,
        "citations": ["Source 1"],
        "escalate": False,
    }
    result = response_agent.draft_reply(
        "I am locked out",
        provider=FakeLLMProvider(payload),
        confidence_threshold=0.5,
    )

    assert result.needs_review is False
    assert result.reasons == ["auto_reply_ready"]


def test_agent_recommended_escalation_requires_review() -> None:
    payload = {
        "draft": "A specialist will follow up with you.",
        "confidence": 0.9,
        "citations": [],
        "escalate": True,
    }
    result = response_agent.draft_reply(
        "I want to sue the company",
        provider=FakeLLMProvider(payload),
    )

    assert result.needs_review is True
    assert "agent_recommended_escalation" in result.reasons


def test_guardrail_violations_flag_draft() -> None:
    payload = {
        "draft": "Please wait, I need to [insert explanation] for you.",
        "confidence": 0.9,
        "citations": ["Source 1"],
        "escalate": False,
    }
    result = response_agent.draft_reply(
        "I am locked out",
        provider=FakeLLMProvider(payload),
    )

    assert result.needs_review is True
    assert "guardrail_violations" in result.reasons
    assert any(violation.rule_id == "response.placeholder" for violation in result.guardrail_violations)


def test_no_knowledge_matches_uses_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(response_agent, "search_knowledge", lambda message, limit=5: [])
    result = response_agent.draft_reply(
        "completely ungrounded gibberish",
        provider=FakeLLMProvider({}),
    )

    assert result.needs_review is True
    assert result.reasons == ["no_knowledge_matches"]
    assert result.draft == response_agent.NO_KB_FALLBACK
    assert result.confidence == 0.0
    assert result.citations == []


def test_invalid_json_from_provider_yields_empty_draft() -> None:
    result = response_agent.draft_reply(
        "I am locked out",
        provider=FakeLLMProvider(text="this is not json"),
    )

    assert result.needs_review is True
    assert "empty_draft" in result.reasons
    assert result.provider == "fake"


# --- the draft agent's policy for a contract failure --------------------


def _contract_failure(payload_text: str, recorded: list[dict]):
    result = response_agent.draft_reply(
        "I am locked out",
        provider=FakeLLMProvider(text=payload_text),
    )
    return result


@pytest.fixture
def recorded_invalid_responses():
    llm.reset_llm_usage()
    llm.reset_rate_limits()
    recorded: list[dict] = []
    llm.set_invalid_response_sink(recorded.append)
    yield recorded
    llm.reset_llm_usage()
    llm.reset_rate_limits()
    llm.set_invalid_response_sink(None)


@pytest.mark.parametrize(
    "text",
    ["this is not json", '{"draft": "half', '["a list"]', ""],
    ids=["prose", "truncated", "json_array", "empty"],
)
def test_a_broken_payload_forces_review_and_names_the_cause(
    text: str, recorded_invalid_responses: list[dict]
) -> None:
    """This path answers a customer, so the worst outcome is a ticket in front
    of a human - never a 500 on ticket creation. It must also say *why*: the
    old code produced `empty_draft` for both "the model returned nothing" and
    "the model returned prose", which reads identically in the review trail."""
    result = _contract_failure(text, recorded_invalid_responses)

    assert result.needs_review is True
    assert result.reasons == ["invalid_llm_response", "empty_draft"]
    assert result.draft == ""
    assert result.confidence == 0.91, "retrieval score, as for any absent confidence"
    assert recorded_invalid_responses[0]["contract"] == "response.draft_json"


def test_a_contract_failure_still_names_the_provider_and_model(
    recorded_invalid_responses: list[dict]
) -> None:
    """A review record with no provider on it cannot be acted on - staff need
    to know which model to go and look at."""
    result = _contract_failure("not json", recorded_invalid_responses)

    assert result.provider == "fake"
    assert result.model == "fake-model"


def test_the_draft_contract_is_total_so_only_parse_failures_are_refused(
    recorded_invalid_responses: list[dict]
) -> None:
    """Every field coerces, so this contract can only refuse a payload that is
    not a JSON object at all.

    A stricter contract would report ordinary model vagueness as a provider
    failure and bury the real parse failures - the review trail is only useful
    if `invalid_llm_response` means something.
    """
    for payload in (
        {"draft": "ok", "confidence": "high", "citations": "Source 1", "escalate": "maybe"},
        {"draft": 42, "confidence": [1], "citations": {"a": 1}, "escalate": None},
        {},
    ):
        result = response_agent.draft_reply("I am locked out", provider=FakeLLMProvider(payload))
        assert "invalid_llm_response" not in result.reasons

    assert recorded_invalid_responses == []


def test_a_non_string_draft_is_coerced_rather_than_refused(
    recorded_invalid_responses: list[dict]
) -> None:
    """A numeric `draft` is not worth escalating on its own - it becomes an
    empty draft, which already forces review, so recording it as a provider
    contract failure would only add noise."""
    result = response_agent.draft_reply(
        "I am locked out",
        provider=FakeLLMProvider({"draft": 42, "confidence": 0.9}),
    )

    assert result.draft == ""
    assert result.reasons == ["empty_draft"]
    assert recorded_invalid_responses == []


def test_the_content_alias_still_works() -> None:
    """`content` was the shape an older prompt asked for; the contract keeps
    accepting it rather than turning a working provider into a failure."""
    result = response_agent.draft_reply(
        "I am locked out",
        provider=FakeLLMProvider({"content": "Please use the reset flow.", "confidence": 0.9}),
    )

    assert result.draft == "Please use the reset flow."
    assert result.needs_review is False


def test_a_string_confidence_does_not_fail_the_contract(
    recorded_invalid_responses: list[dict]
) -> None:
    """An out-of-range or non-numeric confidence falls back to the retrieval
    score by design, so it must not be logged as a broken provider."""
    result = response_agent.draft_reply(
        "I am locked out",
        provider=FakeLLMProvider({"draft": "ok", "confidence": "quite sure"}),
    )

    assert result.confidence == 0.91
    assert recorded_invalid_responses == []


def test_a_numeric_string_confidence_is_still_honoured(
    recorded_invalid_responses: list[dict]
) -> None:
    result = response_agent.draft_reply(
        "I am locked out",
        provider=FakeLLMProvider({"draft": "ok", "confidence": "0.9"}),
    )

    assert result.confidence == 0.9
    assert recorded_invalid_responses == []


def test_a_non_list_citations_value_is_ignored_not_refused(
    recorded_invalid_responses: list[dict]
) -> None:
    """One unusable citation label must not lose the whole draft."""
    result = response_agent.draft_reply(
        "I am locked out",
        provider=FakeLLMProvider({"draft": "ok", "confidence": 0.9, "citations": "Source 1"}),
    )

    assert result.citations == []
    assert result.draft == "ok"
    assert recorded_invalid_responses == []


def test_escalate_accepts_the_string_form() -> None:
    """Real providers answer `"true"`; treating that as false would let a draft
    the model itself flagged for escalation go out unchecked."""
    result = response_agent.draft_reply(
        "I am locked out",
        provider=FakeLLMProvider({"draft": "ok", "confidence": 0.9, "escalate": "true"}),
    )

    assert result.needs_review is True
    assert "agent_recommended_escalation" in result.reasons


def test_confidence_falls_back_to_retrieval_score() -> None:
    payload = {
        "draft": "Please reset your password via the reset flow.",
        "citations": ["Source 1"],
        "escalate": False,
    }
    result = response_agent.draft_reply(
        "I am locked out",
        provider=FakeLLMProvider(payload),
    )

    assert result.confidence == 0.91
    assert result.needs_review is False


def test_out_of_range_confidence_falls_back_to_retrieval() -> None:
    result = response_agent.draft_reply(
        "I am locked out",
        provider=FakeLLMProvider({"draft": "ok", "confidence": 5, "escalate": False}),
    )

    assert result.confidence == 0.91


def test_unmatched_citation_labels_are_ignored() -> None:
    result = response_agent.draft_reply(
        "I am locked out",
        provider=FakeLLMProvider(
            {"draft": "ok", "confidence": 0.8, "citations": ["Source 9"], "escalate": False}
        ),
    )

    assert result.citations == []
    assert result.needs_review is False