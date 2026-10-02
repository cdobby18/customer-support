"""The shared JSON-contract layer in `app/agents/llm.py`.

Before this, each agent parsed the
provider's reply itself and each one failed differently: triage raised, the
draft agent substituted an empty object, agent assist swallowed it. Nothing
recorded that a completion had been unusable, so `LLMUsage` counted a broken
answer as a success and no audit row existed.

These tests pin the *mechanism* (typed refusal, audit row, counters). The
per-agent policies live in `test_triage_contract.py`, `test_response_agent.py`
and `test_agent_assist.py`.
"""

import json

import pytest
from pydantic import BaseModel

from app.agents import llm, prompts
from app.agents.triage import TriageClassification


class TextProvider(llm.LLMProvider):
    """A provider that answers with a fixed string, however malformed."""

    name = "text"

    def __init__(self, text: str, model: str = "text-model") -> None:
        self._text = text
        self._model = model

    def validate_config(self) -> None:
        return None

    def complete(self, messages, *, temperature=0.2, max_tokens=600, response_format=None):
        return llm.LLMResult(
            text=self._text,
            model=self._model,
            provider=self.name,
            usage=llm.LLMUsage(prompt_tokens=7, completion_tokens=3, total_tokens=10),
        )


class Sample(BaseModel):
    label: str
    count: int = 0


@pytest.fixture(autouse=True)
def clean_state():
    """No cross-test leakage of usage counters, recorded refusals or sinks."""
    llm.reset_llm_usage()
    llm.reset_rate_limits()
    recorded: list[dict] = []
    llm.set_invalid_response_sink(recorded.append)
    yield recorded
    llm.reset_llm_usage()
    llm.reset_rate_limits()
    llm.set_invalid_response_sink(None)


def contract_call(provider, **kwargs):
    return llm.generate_structured(
        provider, "prompt text", contract=Sample, contract_name="sample", **kwargs
    )


# --- the refusal itself -------------------------------------------------


def test_a_valid_payload_is_returned_with_its_provider_identity() -> None:
    completion = contract_call(TextProvider(json.dumps({"label": "ok", "count": 2})))

    assert completion.data.label == "ok"
    assert completion.data.count == 2
    assert completion.provider == "text"
    assert completion.model == "text-model"


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("not json at all", "unparseable_json"),
        ('{"label": "ok"', "unparseable_json"),
        ("[1, 2, 3]", "not_an_object"),
        ('"a bare string"', "not_an_object"),
        ("", "empty_response"),
        ("   ", "empty_response"),
        # Truncated by max_tokens mid-object: the single most common way a
        # real provider breaks a contract.
        ('{"label": "ok", "cou', "unparseable_json"),
    ],
)
def test_an_unusable_payload_is_a_typed_refusal(text: str, reason: str) -> None:
    with pytest.raises(llm.LLMContractError) as excinfo:
        contract_call(TextProvider(text))

    assert reason in str(excinfo.value)
    assert excinfo.value.contract == "sample"
    assert excinfo.value.provider == "text"


def test_a_schema_violation_is_refused_with_the_offending_detail() -> None:
    with pytest.raises(llm.LLMContractError, match="schema_violation") as excinfo:
        contract_call(TextProvider(json.dumps({"label": "ok", "count": "many"})))

    assert "count" in str(excinfo.value)


def test_a_missing_required_field_is_a_schema_violation() -> None:
    with pytest.raises(llm.LLMContractError, match="schema_violation"):
        contract_call(TextProvider(json.dumps({"count": 1})))


def test_a_fenced_json_reply_is_unwrapped_rather_than_refused() -> None:
    """`response_format` is a request, not a guarantee.

    Azure OpenAI deployments still answer in fenced blocks often enough that
    refusing to unwrap fails the contract over whitespace.
    """
    completion = contract_call(
        TextProvider('```json\n{"label": "fenced"}\n```')
    )

    assert completion.data.label == "fenced"


def test_a_fence_mention_inside_a_json_string_is_not_unwrapped() -> None:
    completion = contract_call(
        TextProvider(json.dumps({"label": "use ``` fences", "count": 1}))
    )

    assert completion.data.label == "use ``` fences"


def test_a_reply_that_is_only_a_partial_fence_is_refused() -> None:
    """Only a full-string fence counts; a stray opening fence is junk."""
    with pytest.raises(llm.LLMContractError, match="unparseable_json"):
        contract_call(TextProvider('```json\n{"label": "ok"}'))


# --- the refusal is recorded --------------------------------------------


def test_every_refusal_records_an_invalid_response(clean_state: list[dict]) -> None:
    with pytest.raises(llm.LLMContractError):
        contract_call(TextProvider("not json"))

    assert len(clean_state) == 1
    record = clean_state[0]
    assert record["contract"] == "sample"
    assert record["provider"] == "text"
    assert record["model"] == "text-model"
    assert record["reason"] == "unparseable_json"
    assert record["response_sample"]
    assert record["ts"]


def test_a_valid_payload_records_nothing(clean_state: list[dict]) -> None:
    contract_call(TextProvider(json.dumps({"label": "ok"})))

    assert clean_state == []


def test_the_recorded_sample_is_redacted_and_bounded(clean_state: list[dict]) -> None:
    """The provider was just handed a ticket, so the reply can carry PII.

    The audit log is retained for `AUDIT_LOG_RETENTION_DAYS` (a year by
    default), so the sample is redacted and truncated rather than stored raw.
    """
    with pytest.raises(llm.LLMContractError):
        contract_call(TextProvider("email me at customer@example.com about " + "x" * 900))

    sample = clean_state[0]["response_sample"]
    assert "customer@example.com" not in sample
    assert len(sample) <= 500


def test_a_failing_sink_never_breaks_the_caller(clean_state: list[dict]) -> None:
    """Recording a bad response must not take down the request that is
    already going to degrade."""

    def explode(record):
        raise RuntimeError("audit database is gone")

    llm.set_invalid_response_sink(explode)

    with pytest.raises(llm.LLMContractError, match="unparseable_json"):
        contract_call(TextProvider("not json"))

    # Still counted, even though the audit write failed.
    assert llm.get_invalid_responses()[0]["contract"] == "sample"


def test_the_default_sink_writes_an_llm_invalid_response_audit_row(
    monkeypatch: pytest.MonkeyPatch, clean_state: list[dict]
) -> None:
    """The default sink is what a real deployment uses, and nothing else in the
    suite exercises it - an audit action that is only ever written through an
    injected fake is an audit action nobody has ever seen work."""
    from app.api import schemas
    from app.core import database

    calls: list = []

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def commit(self):
            calls.append("commit")

    def fake_add_audit_log(db, actor_id, action, entity_type, entity_id, details):
        calls.append((actor_id, action, entity_type, entity_id, details))

    monkeypatch.setattr(database, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(schemas, "add_audit_log", fake_add_audit_log)
    llm.set_invalid_response_sink(None)

    with pytest.raises(llm.LLMContractError):
        contract_call(TextProvider("not json", model="drifted-model"))

    assert calls[0][0] is None, "no actor: the gateway, not a user, saw this"
    assert calls[0][1] == "llm.invalid_response"
    assert calls[0][2] == "llm"
    assert calls[0][3] == "sample", "the contract name is the entity"
    assert calls[0][4]["model"] == "drifted-model"
    assert calls[1] == "commit"


# --- and it is counted, not silently absorbed ---------------------------


def test_invalid_responses_are_counted_separately_from_calls() -> None:
    with pytest.raises(llm.LLMContractError):
        contract_call(TextProvider("not json"))

    summary = llm.get_llm_usage_summary()

    # The call happened and the tokens were spent, so it is still a call - but
    # it is no longer indistinguishable from a usable one.
    assert summary["total_calls"] == 1
    assert summary["invalid_responses"] == 1
    assert summary["by_model"][0]["calls"] == 1
    assert summary["by_model"][0]["invalid_responses"] == 1


def test_the_summary_reports_failures_for_a_model_with_no_successful_call() -> None:
    """A model that has only ever failed still has to appear, or the admin
    view would silently omit the model that is broken."""
    contract_call(TextProvider(json.dumps({"label": "ok"}), model="healthy-model"))
    with pytest.raises(llm.LLMContractError):
        contract_call(TextProvider("not json", model="flaky-model"))

    summary = llm.get_llm_usage_summary()

    by_model = {entry["model"]: entry for entry in summary["by_model"]}
    assert by_model["healthy-model"]["invalid_responses"] == 0
    assert by_model["healthy-model"]["calls"] == 1
    assert by_model["flaky-model"]["invalid_responses"] == 1
    assert by_model["flaky-model"]["calls"] == 1


def test_reset_clears_the_invalid_response_counters() -> None:
    with pytest.raises(llm.LLMContractError):
        contract_call(TextProvider("not json"))

    llm.reset_llm_usage()

    assert llm.get_llm_usage_summary()["invalid_responses"] == 0
    assert llm.get_invalid_responses() == []


def test_the_empty_summary_still_reports_failures() -> None:
    llm.reset_llm_usage()
    llm.record_invalid_response({"contract": "sample", "model": "text-model"})

    summary = llm.get_llm_usage_summary()

    assert summary["total_calls"] == 0
    assert summary["invalid_responses"] == 1


# --- the contract is the prompt's, expressed once -----------------------


def test_the_triage_contract_is_the_prompt_key_set() -> None:
    """`TriageClassification` and the rendered template must agree.

    A field added to the contract without being documented in the prompt
    invites the model to omit it; a key promised by the prompt but absent from
    the contract makes a compliant answer look broken.
    """
    rendered = prompts.get_template("triage.classify").render(message="I was charged twice")

    for key in TriageClassification.model_fields:
        assert f"- {key}:" in rendered


def test_an_undocumented_default_does_not_make_a_required_field_optional() -> None:
    assert TriageClassification.model_fields["intent"].is_required()
    assert TriageClassification.model_fields["priority"].is_required()
    assert TriageClassification.model_fields["sentiment"].is_required()
    assert not TriageClassification.model_fields["summary"].is_required()