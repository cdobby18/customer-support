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