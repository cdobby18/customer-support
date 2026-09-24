"""Response Agent: KB-grounded draft replies with guardrails.

Given a customer message the agent retrieves top knowledge-base matches,
renders them into the ``response.draft_json`` prompt, calls the LLM gateway
for a structured draft, validates it through the deterministic guardrail
``validate_response`` layer, and decides whether the draft may be sent
directly or needs human review.

Review is required when the draft is empty, no KB matches were found, the
model confident or retrieval score falls below ``RESPONSE_CONFIDENCE_THRESHOLD``
(default 0.75), guardrail violations are present, or the model recommends
escalation.
"""

import json
import os
from typing import Any

from pydantic import BaseModel, Field

from app.guardrails import Violation, validate_response
from app.knowledge import KnowledgeMatch, search_knowledge
from app.llm import LLMProvider, generate
from app.prompts import get_template

NO_KB_FALLBACK = (
    "I don't have enough information to answer that yet, so a specialist will "
    "review your request and follow up shortly."
)


class DraftCitation(BaseModel):
    document_id: str
    title: str
    source: str
    score: float = Field(ge=0)


class DraftResult(BaseModel):
    draft: str
    citations: list[DraftCitation] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    needs_review: bool
    reasons: list[str] = Field(default_factory=list)
    guardrail_violations: list[Violation] = Field(default_factory=list)
    provider: str = ""
    model: str = ""
    template_version: int


def _render_excerpts(matches: list[KnowledgeMatch]) -> tuple[str, dict[str, DraftCitation]]:
    lines: list[str] = []
    citation_map: dict[str, DraftCitation] = {}
    for index, match in enumerate(matches, start=1):
        label = f"Source {index}"
        citation_map[label] = DraftCitation(
            document_id=match.document_id,
            title=match.title,
            source=match.source,
            score=round(match.score, 3),
        )
        lines.append(f"{label} ({match.source}): {match.excerpt}")
    return "\n".join(lines), citation_map


def _match_citations(raw: Any, citation_map: dict[str, DraftCitation]) -> list[DraftCitation]:
    if not isinstance(raw, list):
        return []
    citations: list[DraftCitation] = []
    seen: set[str] = set()
    for item in raw:
        label = str(item).strip()
        citation = citation_map.get(label)
        if citation is not None and citation.document_id not in seen:
            seen.add(citation.document_id)
            citations.append(citation)
    return citations


def _parse_confidence(raw: Any, matches: list[KnowledgeMatch]) -> float:
    if isinstance(raw, (int, float)):
        value = float(raw)
        if 0.0 <= value <= 1.0:
            return round(value, 3)
    if not matches:
        return 0.0
    best = max(match.score for match in matches)
    return round(max(0.0, min(1.0, best)), 3)


def _decision(
    draft_text: str,
    matches: list[KnowledgeMatch],
    confidence: float,
    escalate: bool,
    violations: list[Violation],
    threshold: float,
) -> tuple[list[str], bool]:
    reasons: list[str] = []
    if not matches:
        reasons.append("no_knowledge_matches")
    if not draft_text:
        reasons.append("empty_draft")
    elif confidence < threshold:
        reasons.append("low_confidence")
    if violations:
        reasons.append("guardrail_violations")
    if escalate:
        reasons.append("agent_recommended_escalation")
    if not reasons:
        reasons.append("auto_reply_ready")
    return reasons, reasons != ["auto_reply_ready"]


def draft_reply(
    message: str,
    *,
    provider: LLMProvider | str | None = None,
    limit_kb: int = 5,
    confidence_threshold: float | None = None,
) -> DraftResult:
    if confidence_threshold is not None:
        threshold = float(confidence_threshold)
    else:
        threshold = float(os.getenv("RESPONSE_CONFIDENCE_THRESHOLD", "0.75"))
    matches = search_knowledge(message, limit=limit_kb)
    template = get_template("response.draft_json")

    if not matches:
        return DraftResult(
            draft=NO_KB_FALLBACK,
            citations=[],
            confidence=0.0,
            needs_review=True,
            reasons=["no_knowledge_matches"],
            guardrail_violations=[],
            provider="",
            model="",
            template_version=template.version,
        )

    excerpts, citation_map = _render_excerpts(matches)
    system = template.render(excerpts=excerpts)
    result = generate(
        provider,
        message,
        system=system,
        temperature=0.2,
        max_tokens=700,
        response_format="json_object",
    )

    parsed: dict[str, Any] = {}
    try:
        candidate = json.loads(result.text)
        if isinstance(candidate, dict):
            parsed = candidate
    except json.JSONDecodeError:
        parsed = {}

    draft_text = str(parsed.get("draft") or parsed.get("content") or "").strip()
    escalate = parsed.get("escalate") is True
    citations = _match_citations(parsed.get("citations"), citation_map)
    confidence = _parse_confidence(parsed.get("confidence"), matches)
    violations = validate_response(draft_text)
    reasons, needs_review = _decision(
        draft_text,
        matches,
        confidence,
        escalate,
        violations,
        threshold,
    )

    return DraftResult(
        draft=draft_text,
        citations=citations,
        confidence=confidence,
        needs_review=needs_review,
        reasons=reasons,
        guardrail_violations=violations,
        provider=result.provider,
        model=result.model,
        template_version=template.version,
    )