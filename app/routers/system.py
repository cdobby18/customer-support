"""Health check and knowledge base search."""

from fastapi import APIRouter, Depends, Query

from app.dependencies import get_current_user
from app.knowledge import KnowledgeMatch, search_knowledge
from app.models import UserRecord

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/knowledge/search", response_model=list[KnowledgeMatch])
def knowledge_search(
    q: str = Query(min_length=2, max_length=200),
    limit: int = Query(default=5, ge=1, le=10),
    _: UserRecord = Depends(get_current_user),
) -> list[KnowledgeMatch]:
    return search_knowledge(q, limit=limit)
