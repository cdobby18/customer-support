import os
import pickle
from pathlib import Path
from typing import Any

import faiss
import numpy as np
from sentence_transformers import SentenceTransformer


MODEL_NAME = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
CACHE_DIR = Path(__file__).parents[1] / "data" / "embeddings"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

_index: faiss.IndexFlatIP | None = None
_documents: list[dict[str, Any]] | None = None
_model: SentenceTransformer | None = None


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def _get_cache_paths() -> tuple[Path, Path]:
    index_path = CACHE_DIR / "faiss_index.bin"
    docs_path = CACHE_DIR / "documents.pkl"
    return index_path, docs_path


def _load_cached() -> bool:
    global _index, _documents
    index_path, docs_path = _get_cache_paths()
    if index_path.exists() and docs_path.exists():
        try:
            _index = faiss.read_index(str(index_path))
            with open(docs_path, "rb") as f:
                _documents = pickle.load(f)
            return True
        except Exception:
            pass
    return False


def _save_cache() -> None:
    index_path, docs_path = _get_cache_paths()
    if _index is not None:
        faiss.write_index(_index, str(index_path))
    if _documents is not None:
        with open(docs_path, "wb") as f:
            pickle.dump(_documents, f)


def build_index(documents: list[dict[str, Any]]) -> None:
    global _index, _documents
    model = _get_model()
    texts = [doc["content"] for doc in documents]
    embeddings = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    embeddings = np.array(embeddings, dtype=np.float32)

    dim = embeddings.shape[1]
    _index = faiss.IndexFlatIP(dim)
    _index.add(embeddings)
    _documents = documents
    _save_cache()


def ensure_index() -> None:
    if _index is None or _documents is None:
        if not _load_cached():
            from app.knowledge import load_documents

            docs = load_documents()
            documents = [
                {"id": d.id, "title": d.title, "source": d.source, "content": d.content}
                for d in docs
            ]
            build_index(documents)


def semantic_search(query: str, limit: int = 5) -> list[dict[str, Any]]:
    ensure_index()
    if _index is None or _documents is None:
        return []

    model = _get_model()
    query_embedding = model.encode([query], normalize_embeddings=True)
    query_embedding = np.array(query_embedding, dtype=np.float32)

    scores, indices = _index.search(query_embedding, min(limit, len(_documents)))

    results = []
    for score, idx in zip(scores[0], indices[0]):
        if idx == -1:
            continue
        doc = _documents[idx]
        results.append(
            {
                "document_id": doc["id"],
                "title": doc["title"],
                "source": doc["source"],
                "content": doc["content"],
                "score": float(score),
            }
        )
    return results