"""Agent Assist Agent: live support for an in-flight ticket.

Task 7. Builds a compact ticket history, asks the LLM gateway for a summary
and suggested replies using the versioned ``agent_assist.suggest_json``
template, surfaces relevant knowledge-base articles via ``search_knowledge``,
and ranks similar previously resolved/closed tickets by shared-term overlap.
Suggested replies are passed through the deterministic guardrail layer
(``validate_response``) so an agent can see which ones policy would flag.

Every LLM-backed part degrades to a deterministic result when the gateway is
unavailable, so the endpoint keeps working in zero-key dev and tests.
"""

from collections.abc import Iterable
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.guardrails import Violation, validate_response
from app.agents.knowledge import KnowledgeMatch, search_knowledge, tokenize
from app.agents.llm import (
    LLMContractError,
    LLMError,
    LLMNotConfigured,
    LLMProvider,
    generate_structured,
)
from app.agents.prompts import get_template
from app.core.models import TicketRecord

# Tickets in these states are the pool of "similar resolved cases".
RESOLVED_STATES = ("resolved", "closed")
_HISTORY_MESSAGE_LIMIT = 1200
_HISTORY_COMMENT_LIMIT = 400
_MAX_REPLIES = 3

# Common function words that would otherwise make every ticket look similar.
_STOPWORDS = frozenset(
    {
        "about", "after", "again", "all", "also", "and", "any", "are", "been",
        "before", "being", "both", "but", "can", "could", "did", "does", "doing",
        "for", "from", "had", "has", "have", "having", "her", "here", "him",
        "his", "how", "into", "its", "just", "let", "may", "more", "most",
        "much", "need", "not", "now", "off", "once", "only", "other", "our",
        "out", "over", "please", "same", "shall", "she", "should", "some",
        "such", "than", "that", "the", "their", "them", "then", "there",
        "these", "they", "thing", "things", "this", "those", "through",
        "too", "very", "was", "were", "what", "when", "where", "which",
        "while", "who", "whom", "why", "will", "with", "would", "you",
        "your", "yours",
    }
)


class SuggestedReply(BaseModel):
    text: str
    violations: list[Violation] = Field(default_factory=list)


class AssistContract(BaseModel):
    """The `agent_assist.suggest_json` prompt's JSON contract.

    Only `summary` is required, because a summary with no replies is a
    legitimately unhelpful answer while *no* summary at all means the provider
    ignored the prompt. `suggested_replies` tolerates junk items so the agent
    sees the usable ones instead of losing the lot to one bad element, and the
    cap is applied after parsing.
    """

    summary: str
    suggested_replies: list[Any] = Field(default_factory=list)
    recommended_team: str = ""

    @field_validator("summary", mode="before")
    @classmethod
    def _summary_text(cls, value: Any) -> str:
        return value.strip() if isinstance(value, str) else ""

    @field_validator("suggested_replies", mode="before")
    @classmethod
    def _replies_list(cls, value: Any) -> list[Any]:
        return value if isinstance(value, list) else []

    @field_validator("recommended_team", mode="before")
    @classmethod
    def _team_text(cls, value: Any) -> str:
        return value.strip() if isinstance(value, str) else ""


class AssistStatus(str, Enum):
    """Why the agent-assist payload looks the way it does.

    `ok` and `no_suggestions` are the only states that mean "the model
    answered and had nothing to offer". Everything else is a failure the agent
    needs to see, because the panel used to render all of them as the same
    empty list - a provider returning prose where JSON was asked for looked
    identical to a model with nothing useful to say.
    """

    ok = "ok"
    no_suggestions = "no_suggestions"
    not_configured = "not_configured"
    provider_error = "provider_error"
    invalid_response = "invalid_response"


class SimilarCase(BaseModel):
    ticket_id: str
    summary: str
    intent: str
    status: str
    score: float = Field(ge=0, le=1)
    resolved_at: datetime | None = None


class AgentAssistResult(BaseModel):
    ticket_id: str
    summary: str
    suggested_replies: list[SuggestedReply] = Field(default_factory=list)
    similar_cases: list[SimilarCase] = Field(default_factory=list)
    knowledge: list[KnowledgeMatch] = Field(default_factory=list)
    recommended_team: str | None = None
    status: AssistStatus = AssistStatus.ok
    status_detail: str | None = None
    provider: str = ""
    model: str = ""
    template_version: int


def _clip(text: str, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def _ticket_terms(ticket: Any) -> set[str]:
    return tokenize(f"{ticket.triage_summary or ''} {ticket.message}") - _STOPWORDS


def _overlap(query: set[str], candidate: set[str]) -> float:
    """Jaccard-style similarity, 0.0-1.0."""
    if not query or not candidate:
        return 0.0
    union = query | candidate
    return len(query & candidate) / len(union)


def build_history(ticket: Any, comments: Iterable[Any] = ()) -> str:
    """Render a ticket and its public comments into a prompt-ready history.

    Internal notes are excluded: they are staff-only and must not leave the
    system inside a third-party LLM request.
    """
    lines = [
        f"Channel: {ticket.channel}",
        f"Intent: {ticket.intent}",
        f"Priority: {ticket.priority}",
    ]
    if ticket.triage_summary:
        lines.append(f"Triage summary: {_clip(ticket.triage_summary, _HISTORY_COMMENT_LIMIT)}")
    lines.append(f"Customer message: {_clip(ticket.message, _HISTORY_MESSAGE_LIMIT)}")
    for comment in comments:
        if comment.is_internal:
            continue
        author = "Agent" if comment.author_id != ticket.customer_id else "Customer"
        lines.append(f"{author} reply: {_clip(comment.body, _HISTORY_COMMENT_LIMIT)}")
    return "\n".join(lines)


def find_similar_cases(
    db: Session,
    ticket: Any,
    *,
    limit: int = 3,
) -> list[SimilarCase]:
    """Rank other resolved/closed tickets by shared-term overlap."""
    if limit <= 0:
        return []
    terms = _ticket_terms(ticket)
    if not terms:
        return []
    candidates = db.scalars(
        select(TicketRecord).where(
            TicketRecord.id != ticket.id,
            TicketRecord.status.in_(RESOLVED_STATES),
        )
    ).all()

    cases: list[SimilarCase] = []
    for candidate in candidates:
        score = _overlap(terms, _ticket_terms(candidate))
        if score <= 0:
            continue
        cases.append(
            SimilarCase(
                ticket_id=candidate.id,
                summary=_clip(candidate.triage_summary or candidate.message, 240),
                intent=candidate.intent,
                status=candidate.status,
                score=round(score, 3),
                resolved_at=candidate.resolved_at,
            )
        )
    cases.sort(key=lambda case: (-case.score, case.ticket_id))
    return cases[:limit]


def _fallback_summary(ticket: Any) -> str:
    if ticket.triage_summary:
        return _clip(ticket.triage_summary, 400)
    return f"Open {ticket.intent} ticket via {ticket.channel}: {_clip(ticket.message, 400)}"


def _parse_replies(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    replies: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text and text not in replies:
            replies.append(text)
        if len(replies) == _MAX_REPLIES:
            break
    return replies


class _AssistSummary(BaseModel):
    """Internal carrier so the LLM section keeps one return path."""

    summary: str
    replies: list[SuggestedReply] = Field(default_factory=list)
    team: str = ""
    status: AssistStatus = AssistStatus.ok
    status_detail: str | None = None
    provider: str = ""
    model: str = ""


def _summarize_with_llm(
    history: str,
    ticket: Any,
    *,
    provider: LLMProvider | str | None = None,
) -> _AssistSummary:
    """Return the LLM-backed part of the assist payload.

    Every LLM problem degrades to the deterministic summary, but the three
    failure modes are reported distinctly instead of collapsing into an empty
    reply list: an unconfigured provider, a provider that failed, and a
    provider that answered in a shape this agent cannot use. The last one is
    the case worth surfacing loudly - it means the prompt and the provider
    have drifted apart, and an agent reading "no replies suggested" would have
    no reason to suspect that.
    """
    fallback = _fallback_summary(ticket)
    template = get_template("agent_assist.suggest_json")
    system = template.render(history=history)
    try:
        completion = generate_structured(
            provider,
            "Summarize this ticket and suggest replies for the agent.",
            system=system,
            temperature=0.2,
            max_tokens=800,
            contract=AssistContract,
            contract_name="agent_assist.suggest_json",
        )
    except LLMNotConfigured:
        return _AssistSummary(
            summary=fallback,
            status=AssistStatus.not_configured,
            status_detail="No LLM provider is configured.",
        )
    except LLMContractError as exc:
        return _AssistSummary(
            summary=fallback,
            status=AssistStatus.invalid_response,
            status_detail=str(exc),
            provider=exc.provider,
            model=exc.model,
        )
    except LLMError as exc:
        return _AssistSummary(
            summary=fallback,
            status=AssistStatus.provider_error,
            status_detail=str(exc),
        )

    payload = completion.data
    replies = [
        SuggestedReply(text=text, violations=validate_response(text))
        for text in _parse_replies(payload.suggested_replies)
    ]
    return _AssistSummary(
        summary=payload.summary or fallback,
        replies=replies,
        team=payload.recommended_team,
        status=AssistStatus.ok if replies else AssistStatus.no_suggestions,
        provider=completion.provider,
        model=completion.model,
    )


def assist_ticket(
    db: Session,
    ticket: Any,
    comments: Iterable[Any] = (),
    *,
    provider: LLMProvider | str | None = None,
    similar_limit: int = 3,
    kb_limit: int = 5,
) -> AgentAssistResult:
    """Assemble the full agent-assist payload for one ticket."""
    comments = list(comments)
    history = build_history(ticket, comments)
    assist = _summarize_with_llm(history, ticket, provider=provider)
    query = _clip(ticket.message, _HISTORY_MESSAGE_LIMIT)

    return AgentAssistResult(
        ticket_id=ticket.id,
        summary=assist.summary,
        suggested_replies=assist.replies,
        similar_cases=find_similar_cases(db, ticket, limit=similar_limit),
        knowledge=search_knowledge(query, limit=kb_limit) if kb_limit > 0 else [],
        recommended_team=assist.team or None,
        status=assist.status,
        status_detail=assist.status_detail,
        provider=assist.provider,
        model=assist.model,
        template_version=get_template("agent_assist.suggest_json").version,
    )
