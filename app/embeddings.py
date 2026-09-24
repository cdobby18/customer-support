"""Provider-aware semantic search over the markdown knowledge base.

Vectors live in a FAISS inner-product index (cosine similarity, normalized
embeddings) cached under ``data/embeddings``. The embedding step is
provider-neutral and driven by ``EMBEDDING_PROVIDER``:
- ``local`` (default): sentence-transformers, no API key required
- ``openai``: OpenAI embeddings API (``text-embedding-3-small`` by default),
  requires ``OPENAI_API_KEY``

The cache stores the provider + model signature that produced it; whenever
the active signature changes (for example switching from a 384-dim local
model to the 1536-dim OpenAI model) the index is rebuilt automatically.
"""

import json
import os
import pickle
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen

import faiss
import numpy as np

LOCAL_MODEL_DEFAULT = "all-MiniLM-L6-v2"
OPENAI_EMBEDDING_DEFAULT = "text-embedding-3-small"

CACHE_DIR = Path(__file__).parents[1] / "data" / "embeddings"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


class EmbeddingError(Exception):
    """Base error for the embedding layer."""


class EmbeddingConfigError(EmbeddingError):
    """Raised when an embedding provider is misconfigured."""


class EmbeddingProvider(ABC):
    name = "embedding"

    @property
    @abstractmethod
    def model(self) -> str:
        """Current model name for this provider."""

    def validate_config(self) -> None:
        raise EmbeddingConfigError(f"{self.name} embedding provider is not configured")

    @abstractmethod
    def embed(self, texts: list[str]) -> np.ndarray:
        """Return an L2-normalized float32 matrix of shape (n, dim)."""


def _l2_normalize(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vectors / norms


def _default_http_request(
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None,
) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(url, data=body, headers=headers, method=method)
    with urlopen(request, timeout=30) as response:
        raw = response.read()
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
        return response.status, parsed


_local_models: dict[str, Any] = {}


def _local_model(name: str) -> Any:
    cached = _local_models.get(name)
    if cached is None:
        from sentence_transformers import SentenceTransformer

        cached = SentenceTransformer(name)
        _local_models[name] = cached
    return cached


class LocalEmbeddingProvider(EmbeddingProvider):
    name = "local"

    @property
    def model(self) -> str:
        return os.getenv("EMBEDDING_MODEL", LOCAL_MODEL_DEFAULT).strip()

    def validate_config(self) -> None:
        return None

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        model = _local_model(self.model)
        vectors = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return np.asarray(vectors, dtype=np.float32)


class OpenAIEmbeddingProvider(EmbeddingProvider):
    name = "openai"

    def __init__(self, http_request: Callable[..., Any] | None = None) -> None:
        self._http_request = http_request or _default_http_request

    @property
    def model(self) -> str:
        return os.getenv("EMBEDDING_MODEL", OPENAI_EMBEDDING_DEFAULT).strip()

    def validate_config(self) -> None:
        if not os.getenv("OPENAI_API_KEY"):
            raise EmbeddingConfigError("OpenAI embedding provider requires OPENAI_API_KEY")

    def _endpoint(self) -> str:
        base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        return f"{base_url}/embeddings"

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        batch_size = max(1, int(os.getenv("EMBEDDING_BATCH_SIZE", "64")))
        batches: list[np.ndarray] = []
        for start in range(0, len(texts), batch_size):
            batches.append(self._embed_batch(texts[start : start + batch_size]))
        return np.concatenate(batches, axis=0)

    def _embed_batch(self, texts: list[str]) -> np.ndarray:
        payload = {"model": self.model, "input": list(texts)}
        headers = {
            "Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}",
            "Content-Type": "application/json",
        }
        try:
            status, body = self._http_request("POST", self._endpoint(), headers, payload)
        except Exception as exc:
            raise EmbeddingError(f"openai embeddings transport failure: {exc}") from exc
        if status >= 400:
            error_message = None
            if isinstance(body, dict):
                error_payload = body.get("error")
                if isinstance(error_payload, dict):
                    error_message = error_payload.get("message")
            raise EmbeddingError(
                f"openai embeddings API error status {status}: {error_message or body}"
            )
        data = sorted(body.get("data", []), key=lambda item: item.get("index", 0))
        vectors = np.asarray([item["embedding"] for item in data], dtype=np.float32)
        return _l2_normalize(vectors)


PROVIDERS: dict[str, type[EmbeddingProvider]] = {
    "local": LocalEmbeddingProvider,
    "openai": OpenAIEmbeddingProvider,
}


def get_embedding_provider() -> EmbeddingProvider:
    provider_name = os.getenv("EMBEDDING_PROVIDER", "local").strip().lower()
    provider_class = PROVIDERS.get(provider_name)
    if provider_class is None:
        raise EmbeddingConfigError(f"unknown EMBEDDING_PROVIDER {provider_name!r}")
    return provider_class()


_index: faiss.IndexFlatIP | None = None
_documents: list[dict[str, Any]] | None = None


def _get_cache_paths() -> tuple[Path, Path]:
    return CACHE_DIR / "faiss_index.bin", CACHE_DIR / "documents.pkl"


def _signature(provider: EmbeddingProvider) -> dict[str, str]:
    return {"provider": provider.name, "model": provider.model}


def _save_cache(signature: dict[str, str]) -> None:
    index_path, docs_path = _get_cache_paths()
    if _index is None or _documents is None:
        return
    faiss.write_index(_index, str(index_path))
    with open(docs_path, "wb") as f:
        pickle.dump({"signature": signature, "documents": _documents}, f)


def _load_cached(signature: dict[str, str]) -> bool:
    global _index, _documents
    index_path, docs_path = _get_cache_paths()
    if not (index_path.exists() and docs_path.exists()):
        return False
    try:
        with open(docs_path, "rb") as f:
            payload = pickle.load(f)
        if not isinstance(payload, dict) or payload.get("signature") != signature:
            return False
        _index = faiss.read_index(str(index_path))
        _documents = payload["documents"]
        return True
    except Exception:
        return False


def build_index(documents: list[dict[str, Any]]) -> None:
    global _index, _documents
    provider = get_embedding_provider()
    provider.validate_config()
    signature = _signature(provider)
    texts = [doc["content"] for doc in documents]
    vectors = provider.embed(texts)

    dim = vectors.shape[1]
    index = faiss.IndexFlatIP(dim)
    index.add(vectors)

    _index = index
    _documents = documents
    _save_cache(signature)


def _ensure_index(signature: dict[str, str]) -> None:
    global _index, _documents
    if _index is not None and _documents is not None:
        return
    if _load_cached(signature):
        return
    from app.knowledge import load_documents

    docs = load_documents()
    documents = [
        {"id": d.id, "title": d.title, "source": d.source, "content": d.content}
        for d in docs
    ]
    build_index(documents)


def ensure_index() -> None:
    provider = get_embedding_provider()
    provider.validate_config()
    _ensure_index(_signature(provider))


def semantic_search(query: str, limit: int = 5) -> list[dict[str, Any]]:
    provider = get_embedding_provider()
    provider.validate_config()
    signature = _signature(provider)
    _ensure_index(signature)
    if _index is None or _documents is None:
        return []

    query_vector = provider.embed([query])
    if query_vector.shape[1] != _index.d:
        return []

    scores, indices = _index.search(query_vector, min(limit, len(_documents)))

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