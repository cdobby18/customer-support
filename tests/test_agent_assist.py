import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.agents import agent_assist, llm
from app.agents.knowledge import KnowledgeMatch
from app.core.database import Base
from app.core.models import TicketCommentRecord, TicketRecord

from uuid import uuid4


def kb_matches() -> list[KnowledgeMatch]:
    return [
        KnowledgeMatch(
            document_id="password-reset",
            title="Password reset",
            source="password-reset.md",
            excerpt="Use the password reset flow.",
            score=0.88,
        )
    ]


class FakeLLMProvider(llm.LLMProvider):
    name = "fake"

    def __init__(self, payload: dict | None = None, *, text: str | None = None):
        self._text = text if text is not None else json.dumps(payload or {})

    def validate_config(self) -> None:
        return None

    def complete(self, messages, *, temperature=0.2, max_tokens=600, response_format=None):
        return llm.LLMResult(
            text=self._text,
            model="fake-model",
            provider=self.name,
            usage=llm.LLMUsage(total_tokens=1),
        )


@pytest.fixture(autouse=True)
def db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        yield session
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def isolate_kb(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(agent_assist, "search_knowledge", lambda message, limit=5: kb_matches())


@pytest.fixture(autouse=True)
def no_llm(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    llm.reset_rate_limits()
    llm.reset_llm_usage()


def add_ticket(
    db: Session,
    *,
    message: str,
    status: str = "open",
    triage_summary: str | None = None,
    intent: str = "technical_support",
    ticket_id: str | None = None,
) -> TicketRecord:
    record = TicketRecord(
        id=ticket_id or str(uuid4()),
        customer_id="customer-1",
        message=message,
        channel="web",
        intent=intent,
        priority="normal",
        sentiment="neutral",
        confidence=0.9,
        triage_summary=triage_summary,
        status=status,
        created_at=datetime(2026, 9, 24, tzinfo=timezone.utc),
        updated_at=datetime(2026, 9, 24, tzinfo=timezone.utc),
    )
    if status in agent_assist.RESOLVED_STATES:
        record.resolved_at = datetime(2026, 9, 25, tzinfo=timezone.utc)
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def test_assist_returns_summary_replies_and_team(db: Session) -> None:
    ticket = add_ticket(db, message="I cannot log in to my account")
    payload = {
        "summary": "Customer is locked out of their account.",
        "suggested_replies": ["Please use the password reset link."],
        "recommended_team": "account_support",
    }

    result = agent_assist.assist_ticket(db, ticket, provider=FakeLLMProvider(payload))

    assert result.ticket_id == ticket.id
    assert result.summary == payload["summary"]
    assert [reply.text for reply in result.suggested_replies] == payload["suggested_replies"]
    assert result.suggested_replies[0].violations == []
    assert result.recommended_team == "account_support"
    assert result.provider == "fake"
    assert result.model == "fake-model"
    assert result.template_version == 1
    assert [match.document_id for match in result.knowledge] == ["password-reset"]


def test_assist_falls_back_when_llm_not_configured(db: Session) -> None:
    ticket = add_ticket(db, message="Refund please", triage_summary="Customer wants a refund")

    result = agent_assist.assist_ticket(db, ticket)

    assert result.summary == "Customer wants a refund"
    assert result.suggested_replies == []
    assert result.recommended_team is None
    assert result.provider == ""
    assert result.model == ""


def test_assist_falls_back_to_message_when_no_triage_summary(db: Session) -> None:
    ticket = add_ticket(db, message="The export button does nothing at all")

    result = agent_assist.assist_ticket(db, ticket)

    assert "export button" in result.summary


def test_assist_survives_invalid_json(db: Session) -> None:
    ticket = add_ticket(db, message="Broken", triage_summary="App is broken")

    result = agent_assist.assist_ticket(
        db, ticket, provider=FakeLLMProvider(text="not json at all")
    )

    assert result.summary == "App is broken"
    assert result.suggested_replies == []


# --- task 9: an unusable model shape must not look like an unhelpful one


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


def test_a_compliant_payload_reports_ok(db: Session, recorded_invalid_responses) -> None:
    ticket = add_ticket(db, message="I cannot log in")
    payload = {"summary": "Locked out.", "suggested_replies": ["Use the reset link."]}

    result = agent_assist.assist_ticket(db, ticket, provider=FakeLLMProvider(payload))

    assert result.status is agent_assist.AssistStatus.ok
    assert result.status_detail is None


def test_a_model_that_offers_no_replies_is_not_a_failure(db: Session) -> None:
    """A valid answer with no suggestions is a legitimate outcome and must not
    be dressed up as an error - otherwise the loud path stops meaning anything.
    """
    ticket = add_ticket(db, message="I cannot log in")

    result = agent_assist.assist_ticket(
        db, ticket, provider=FakeLLMProvider({"summary": "Locked out."})
    )

    assert result.status is agent_assist.AssistStatus.no_suggestions
    assert result.status_detail is None
    assert result.suggested_replies == []


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("not json at all", "unparseable_json"),
        ('{"summary": "trunc', "unparseable_json"),
        ('["a list"]', "not_an_object"),
        ("", "empty_response"),
        # Valid JSON, but no summary: the model ignored the prompt's core
        # instruction, so its "no replies" means nothing.
        ('{"suggested_replies": ["Try the reset link."]}', "schema_violation"),
    ],
    ids=["prose", "truncated", "json_array", "empty", "no_summary"],
)
def test_an_unusable_shape_is_reported_loudly_with_the_reason(
    db: Session, recorded_invalid_responses, text: str, reason: str
) -> None:
    """The bug: `_summarize_with_llm` set `parsed = {}` and carried on, so a
    provider answering prose looked exactly like a model with nothing to say
    and the panel said "no replies suggested" for both.
    """
    ticket = add_ticket(db, message="Broken", triage_summary="App is broken")

    result = agent_assist.assist_ticket(db, ticket, provider=FakeLLMProvider(text=text))

    assert result.status is agent_assist.AssistStatus.invalid_response
    assert reason in result.status_detail
    assert result.suggested_replies == []
    # The deterministic summary is still returned, so the panel is usable.
    assert result.summary == "App is broken"
    assert recorded_invalid_responses[0]["contract"] == "agent_assist.suggest_json"
    assert recorded_invalid_responses[0]["reason"] == reason


def test_an_unusable_shape_still_names_the_provider_and_model(
    db: Session, recorded_invalid_responses
) -> None:
    """The whole point of the loud failure is that staff can go and look at
    the model that drifted."""
    ticket = add_ticket(db, message="Broken")

    result = agent_assist.assist_ticket(db, ticket, provider=FakeLLMProvider(text="nope"))

    assert result.provider == "fake"
    assert result.model == "fake-model"


def test_a_missing_provider_is_distinguished_from_a_broken_one(db: Session) -> None:
    ticket = add_ticket(db, message="Refund please", triage_summary="Customer wants a refund")

    result = agent_assist.assist_ticket(db, ticket)

    assert result.status is agent_assist.AssistStatus.not_configured
    assert "No LLM provider" in result.status_detail
    assert result.provider == ""
    assert result.suggested_replies == []


def test_a_transport_failure_is_distinguished_from_a_broken_shape(
    db: Session, recorded_invalid_responses, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both end in no replies, but one is the provider being down and the other
    is the provider answering in the wrong shape - different things to go and
    look at."""

    class BrokenProvider(llm.LLMProvider):
        name = "broken"

        def validate_config(self) -> None:
            return None

        def complete(self, messages, **kwargs):
            raise llm.LLMRetryableError("connection reset")

    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    ticket = add_ticket(db, message="Refund please")

    result = agent_assist.assist_ticket(db, ticket, provider=BrokenProvider())

    assert result.status is agent_assist.AssistStatus.provider_error
    assert "connection reset" in result.status_detail
    assert result.suggested_replies == []
    assert recorded_invalid_responses == []


def test_the_status_survives_the_api_contract(db: Session) -> None:
    """The panel's copy keys off `status`, so it has to be in the response."""
    ticket = add_ticket(db, message="I cannot log in")

    result = agent_assist.assist_ticket(db, ticket, provider=FakeLLMProvider(text="nope"))
    dumped = result.model_dump()

    assert dumped["status"] == "invalid_response"
    assert dumped["status_detail"]


def test_suggested_replies_are_guardrail_validated(db: Session) -> None:
    ticket = add_ticket(db, message="Where do I find my receipt?")
    payload = {
        "summary": "Customer wants a receipt.",
        "suggested_replies": [
            "Receipts are available from the billing page in your account.",
            "Here is the fix: https://evil.example.org/steal",
        ],
    }

    result = agent_assist.assist_ticket(db, ticket, provider=FakeLLMProvider(payload))

    assert result.suggested_replies[0].violations == []
    flagged = result.suggested_replies[1].violations
    assert [violation.category for violation in flagged] == ["external_link"]


def test_suggested_reply_with_email_is_flagged_as_pii(db: Session) -> None:
    ticket = add_ticket(db, message="Who do I contact about a refund?")
    payload = {
        "summary": "Customer wants a contact address.",
        "suggested_replies": ["Email is support@example.com if you need help."],
    }

    result = agent_assist.assist_ticket(db, ticket, provider=FakeLLMProvider(payload))

    assert [violation.category for violation in result.suggested_replies[0].violations] == [
        "email_address"
    ]


def test_replies_are_deduplicated_and_capped(db: Session) -> None:
    ticket = add_ticket(db, message="Need help")
    payload = {
        "summary": "Summary",
        "suggested_replies": ["Same reply", "Same reply", "Second", "Third", "Fourth"],
    }

    result = agent_assist.assist_ticket(db, ticket, provider=FakeLLMProvider(payload))

    assert [reply.text for reply in result.suggested_replies] == [
        "Same reply",
        "Second",
        "Third",
    ]


def test_non_list_replies_are_ignored(db: Session) -> None:
    ticket = add_ticket(db, message="Need help")
    payload = {"summary": "Summary", "suggested_replies": "just a string"}

    result = agent_assist.assist_ticket(db, ticket, provider=FakeLLMProvider(payload))

    assert result.suggested_replies == []


def test_similar_cases_ranked_by_overlap(db: Session) -> None:
    ticket = add_ticket(db, message="I cannot log in to my account after the update")
    strong = add_ticket(
        db,
        message="Cannot log in to my account since the update",
        status="resolved",
        triage_summary="Login broken after update",
    )
    weak = add_ticket(db, message="Where is the invoice history for my account", status="closed")

    cases = agent_assist.find_similar_cases(db, ticket)

    assert [case.ticket_id for case in cases] == [strong.id, weak.id]
    assert cases[0].score > cases[1].score
    assert cases[0].status == "resolved"
    assert cases[0].summary == "Login broken after update"
    assert cases[0].resolved_at is not None


def test_similar_cases_exclude_current_and_unresolved_tickets(db: Session) -> None:
    ticket = add_ticket(
        db,
        message="Refund for my subscription",
        ticket_id="11111111-1111-1111-1111-111111111111",
    )
    add_ticket(
        db,
        message="Totally unrelated invoice address change",
        status="resolved",
    )
    add_ticket(db, message="Refund for my subscription please", status="open")
    add_ticket(db, message="Refund for my subscription now", status="in_progress")

    cases = agent_assist.find_similar_cases(db, ticket)

    assert cases == []


def test_similar_cases_drop_zero_overlap_and_respect_limit(db: Session) -> None:
    ticket = add_ticket(db, message="Password reset link never arrived")
    unrelated = add_ticket(db, message="Invoice address change", status="resolved")
    duplicate = add_ticket(db, message="Password reset link never arrived", status="resolved")

    assert [case.ticket_id for case in agent_assist.find_similar_cases(db, ticket)] == [
        duplicate.id
    ]
    assert unrelated.id not in [case.ticket_id for case in agent_assist.find_similar_cases(db, ticket)]
    assert agent_assist.find_similar_cases(db, ticket, limit=0) == []


def test_similar_cases_ignore_stopword_only_overlap(db: Session) -> None:
    ticket = add_ticket(db, message="The report does not arrive")
    add_ticket(db, message="This is not what was expected", status="resolved")

    assert agent_assist.find_similar_cases(db, ticket) == []


def test_build_history_excludes_internal_notes(db: Session) -> None:
    ticket = add_ticket(db, message="I am locked out", triage_summary="Account access issue")
    db.add(
        TicketCommentRecord(
            id=str(uuid4()),
            ticket_id=ticket.id,
            author_id="agent-1",
            body="Check the internal fraud score",
            is_internal=True,
            created_at=datetime(2026, 9, 24, tzinfo=timezone.utc),
        )
    )
    db.add(
        TicketCommentRecord(
            id=str(uuid4()),
            ticket_id=ticket.id,
            author_id="agent-1",
            body="Please try the reset link",
            is_internal=False,
            created_at=datetime(2026, 9, 24, tzinfo=timezone.utc),
        )
    )
    db.commit()
    comments = db.query(TicketCommentRecord).all()

    history = agent_assist.build_history(ticket, comments)

    assert "Check the internal fraud score" not in history
    assert "Agent reply: Please try the reset link" in history
    assert "Account access issue" in history
    assert "Channel: web" in history


def test_assist_accepts_kb_limit_zero(db: Session) -> None:
    ticket = add_ticket(db, message="Anything")

    result = agent_assist.assist_ticket(db, ticket, kb_limit=0)

    assert result.knowledge == []
