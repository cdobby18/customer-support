import re
from pathlib import Path

from pydantic import BaseModel, Field

from app.agents.embeddings import semantic_search


class KnowledgeDocument(BaseModel):
    id: str
    title: str
    source: str
    content: str


class KnowledgeMatch(BaseModel):
    document_id: str
    title: str
    source: str
    excerpt: str
    score: float = Field(ge=0)


def load_documents(directory: Path | None = None) -> list[KnowledgeDocument]:
    document_directory = directory or Path(__file__).parents[2] / "data" / "docs"
    documents = []
    for path in sorted(document_directory.glob("*.md")):
        content = path.read_text(encoding="utf-8")
        title = content.splitlines()[0].removeprefix("# ").strip() or path.stem
        documents.append(
            KnowledgeDocument(
                id=path.stem,
                title=title,
                source=str(path.relative_to(document_directory)),
                content=content,
            )
        )
    return documents


MIN_MATCH_SCORE = 0.25


def search_knowledge(
    query: str, limit: int = 5, min_score: float = MIN_MATCH_SCORE
) -> list[KnowledgeMatch]:
    results = semantic_search(query, limit=limit)
    matches = []
    for r in results:
        if r["score"] < min_score:
            continue
        excerpt = _excerpt(r["content"], set())
        matches.append(
            KnowledgeMatch(
                document_id=r["document_id"],
                title=r["title"],
                source=r["source"],
                excerpt=excerpt,
                score=round(max(0.0, r["score"]), 3),
            )
        )
    return matches


def tokenize(text: str) -> set[str]:
    """Lowercase word set, dropping 1-2 character noise tokens."""
    return {term for term in re.findall(r"[a-z0-9]+", text.lower()) if len(term) > 2}


def _excerpt(content: str, matching_terms: set[str]) -> str:
    for paragraph in content.split("\n\n"):
        if matching_terms & tokenize(paragraph):
            return " ".join(paragraph.split())[:360]
    return " ".join(content.split())[:360]
