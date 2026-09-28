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

import json
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.guardrails import Violation, validate_response
from app.knowledge import KnowledgeMatch, search_knowledge, tokenize
from app.llm import LLMError, LLMNotConfigured, LLMProvider, generate
from app.models import TicketRecord
from app.prompts import get_template

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


def _summarize_with_llm(
    history: str,
    ticket: Any,
    *,
    provider: LLMProvider | str | None = None,
) -> tuple[str, list[SuggestedReply], str, str, str]:
    """Return (summary, replies, team, provider, model), degrading on LLM errors."""
    fallback = _fallback_summary(ticket)
    template = get_template("agent_assist.suggest_json")
    system = template.render(history=history)
    try:
        result = generate(
            provider,
            "Summarize this ticket and suggest replies for the agent.",
            system=system,
            temperature=0.2,
            max_tokens=800,
            response_format="json_object",
        )
    except (LLMNotConfigured, LLMError):
        return fallback, [], "", "", ""

    try:
        parsed = json.loads(result.text)
        if not isinstance(parsed, dict):
            parsed = {}
    except json.JSONDecodeError:
        parsed = {}

    summary = str(parsed.get("summary") or "").strip() or fallback
    replies = [
        SuggestedReply(text=text, violations=validate_response(text))
        for text in _parse_replies(parsed.get("suggested_replies"))
    ]
    team = str(parsed.get("recommended_team") or "").strip() or None
    return summary, replies, team or "", result.provider, result.model


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
    summary, replies, team, provider_name, model = _summarize_with_llm(
        history, ticket, provider=provider
    )
    query = _clip(ticket.message, _HISTORY_MESSAGE_LIMIT)

    return AgentAssistResult(
        ticket_id=ticket.id,
        summary=summary,
        suggested_replies=replies,
        similar_cases=find_similar_cases(db, ticket, limit=similar_limit),
        knowledge=search_knowledge(query, limit=kb_limit) if kb_limit > 0 else [],
        recommended_team=team or None,
        provider=provider_name,
        model=model,
        template_version=get_template("agent_assist.suggest_json").version,
    )
