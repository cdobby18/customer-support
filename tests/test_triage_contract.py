"""Triage's policy for an unusable model payload: fall back to the classifier.

Task 8 in `markdowns/FUTURE_TASKS.md`. The keyword classifier is a working
deterministic answer, so a provider that drifts away from `triage.classify` must
degrade to it rather than fail the request.

The bug this pins: `_classify_with_llm` used `data["priority"]`,
`data["sentiment"]` and `TriageIntent(data["intent"])` directly. A payload
missing `priority` raised `KeyError` and an unknown intent raised `ValueError`
- neither is an `LLMError`, so neither was caught by `classify_ticket`'s
handler and both escaped as an unhandled 500 on ticket creation.
"""

import json

import pytest

from app.agents import llm, triage


class TextProvider(llm.LLMProvider):
    """Answers with whatever text the subclass's `reply` holds."""

    name = "text"
    reply = "not json"

    def validate_config(self) -> None:
        return None

    def complete(self, messages, *, temperature=0.2, max_tokens=600, response_format=None):
        return llm.LLMResult(text=self.reply, model="text-model", provider=self.name)


@pytest.fixture
def reply(monkeypatch: pytest.MonkeyPatch):
    """Register a provider whose next answer the test controls.

    `classify_ticket` resolves the provider itself, so the registry is the
    only seam - patching `get_provider` would bypass the config check a real
    deployment goes through.
    """

    def install(text: str) -> None:
        llm.PROVIDERS["text"] = type("ScriptedProvider", (TextProvider,), {"reply": text})
        monkeypatch.setenv("LLM_PROVIDER", "text")

    yield install

    llm.PROVIDERS.pop("text", None)


@pytest.fixture(autouse=True)
def clean_state():
    llm.reset_llm_usage()
    llm.reset_rate_limits()
    recorded: list[dict] = []
    llm.set_invalid_response_sink(recorded.append)
    yield recorded
    llm.reset_llm_usage()
    llm.reset_rate_limits()
    llm.set_invalid_response_sink(None)


def valid_payload(**overrides) -> dict:
    payload = {
        "intent": "billing",
        "priority": "high",
        "sentiment": "frustrated",
        "confidence": 0.88,
        "recommended_team": "billing",
        "requires_human_review": True,
        "summary": "Customer was charged twice.",
    }
    payload.update(overrides)
    return payload


def install_json(reply, payload: dict) -> None:
    reply(json.dumps(payload))


# --- the happy path is unchanged ----------------------------------------


def test_a_compliant_payload_is_used_as_classified(reply) -> None:
    install_json(reply, valid_payload())

    result = triage.classify_ticket("I was charged twice")

    assert result.intent is triage.TriageIntent.billing
    assert result.priority is triage.TriagePriority.high
    assert result.sentiment is triage.TriageSentiment.frustrated
    assert result.confidence == pytest.approx(0.88)
    assert result.recommended_team == "billing"
    assert result.requires_human_review is True
    assert result.summary == "Customer was charged twice."


# --- every broken shape degrades to the classifier ----------------------


@pytest.mark.parametrize(
    "payload",
    [
        {},
        # `priority` and `sentiment` were bare subscripts: a `KeyError` here
        # used to escape as a 500.
        {"intent": "billing"},
        {"intent": "billing", "priority": "high"},
    ],
    ids=["empty", "intent_only", "no_sentiment"],
)
def test_a_payload_missing_required_keys_falls_back(
    reply, payload: dict, clean_state: list[dict]
) -> None:
    install_json(reply, payload)

    result = triage.classify_ticket("I was charged twice")

    # The keyword classifier's answer for the same message.
    assert result.intent is triage.TriageIntent.billing
    assert result.priority is triage.TriagePriority.high
    assert result.requires_human_review is True
    assert clean_state[0]["contract"] == "triage.classify"


@pytest.mark.parametrize(
    "payload",
    [
        {"intent": "refund_request"},
        {"intent": "billing", "priority": "immediately"},
        {"intent": "billing", "priority": "high", "sentiment": "furious"},
    ],
    ids=["unknown_intent", "unknown_priority", "unknown_sentiment"],
)
def test_an_out_of_contract_enum_falls_back(
    reply, payload: dict, clean_state: list[dict]
) -> None:
    """The enums *are* the documented key sets, so a value outside them is a
    contract violation rather than a `ValueError` from a constructor."""
    install_json(reply, valid_payload(**payload))

    result = triage.classify_ticket("I was charged twice")

    assert result.intent is triage.TriageIntent.billing
    assert clean_state[0]["reason"] == "schema_violation"


@pytest.mark.parametrize("confidence", [7, -1, "high", None])
def test_an_out_of_range_confidence_falls_back(reply, confidence) -> None:
    install_json(reply, valid_payload(confidence=confidence))

    result = triage.classify_ticket("I was charged twice")

    assert result.intent is triage.TriageIntent.billing
    assert 0.0 <= result.confidence <= 1.0


def test_unparseable_json_falls_back(reply) -> None:
    reply("I'm afraid I can't do that")

    result = triage.classify_ticket("I was charged twice")

    assert result.intent is triage.TriageIntent.billing


def test_a_json_array_is_refused_not_indexed(reply) -> None:
    reply('["billing", "high"]')

    result = triage.classify_ticket("I was charged twice")

    assert result.intent is triage.TriageIntent.billing


def test_a_missing_provider_still_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    result = triage.classify_ticket("I think my card was stolen")

    assert result.intent is triage.TriageIntent.fraud
    assert result.priority is triage.TriagePriority.urgent


# --- the degraded answer is never silently worse ------------------------


def test_an_omitted_summary_falls_back_to_the_message(reply) -> None:
    payload = valid_payload()
    payload.pop("summary")
    install_json(reply, payload)

    result = triage._classify_with_llm("I was charged twice for a plan I cancelled")

    assert result.summary == "I was charged twice for a plan I cancelled"


def test_a_long_message_is_clipped_for_the_fallback_summary(reply) -> None:
    payload = valid_payload()
    payload.pop("summary")
    install_json(reply, payload)

    result = triage._classify_with_llm("charged twice " * 200)

    assert len(result.summary) <= 240


def test_optional_fields_take_their_documented_defaults(reply) -> None:
    install_json(
        reply,
        {
            "intent": "technical_support",
            "priority": "normal",
            "sentiment": "neutral",
        },
    )

    result = triage._classify_with_llm("The export button does nothing")

    assert result.confidence == pytest.approx(0.7)
    assert result.recommended_team == "customer_support"
    assert result.requires_human_review is False
    assert result.summary == "The export button does nothing"