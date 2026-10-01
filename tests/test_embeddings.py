import pickle
import threading
from pathlib import Path

import numpy as np
import pytest

from app.agents import embeddings as emb


@pytest.fixture(autouse=True)
def clean_embedding_state():
    yield
    emb._index = None
    emb._documents = None
    emb._local_models.clear()
    emb.CACHE_DIR.mkdir(parents=True, exist_ok=True)


def test_local_provider_default_model_and_config() -> None:
    provider = emb.LocalEmbeddingProvider()
    provider.validate_config()
    assert provider.name == "local"
    assert provider.model == "all-MiniLM-L6-v2"
    assert provider.embed([]).shape == (0, 0)


def test_local_provider_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_MODEL", "custom-model")
    assert emb.LocalEmbeddingProvider().model == "custom-model"


def test_openai_provider_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(emb.EmbeddingConfigError, match="OPENAI_API_KEY"):
        emb.OpenAIEmbeddingProvider().validate_config()


def test_openai_provider_builds_request_and_normalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-embed")
    monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-3-small")
    captured: dict = {}

    def fake_http_request(method, url, headers, payload):
        captured["method"] = method
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = payload
        return (
            200,
            {
                "data": [{"embedding": [3.0, 4.0], "index": 0}],
                "usage": {"prompt_tokens": 2, "total_tokens": 2},
            },
        )

    provider = emb.OpenAIEmbeddingProvider(http_request=fake_http_request)
    vectors = provider.embed(["hello"])

    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.openai.com/v1/embeddings"
    assert captured["headers"]["Authorization"] == "Bearer sk-embed"
    assert captured["payload"]["model"] == "text-embedding-3-small"
    assert captured["payload"]["input"] == ["hello"]
    assert vectors.shape == (1, 2)
    np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), [1.0])


def test_openai_provider_batches_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-embed")
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "1")
    call_payloads: list[list[str]] = []

    def fake_http_request(method, url, headers, payload):
        inputs = payload["input"]
        call_payloads.append(inputs)
        return (
            200,
            {
                "data": [
                    {"embedding": [1.0, 0.0], "index": i}
                    for i in range(len(inputs))
                ]
            },
        )

    provider = emb.OpenAIEmbeddingProvider(http_request=fake_http_request)
    vectors = provider.embed(["a", "b", "c"])

    assert [len(batch) for batch in call_payloads] == [1, 1, 1]
    assert vectors.shape == (3, 2)


def test_openai_provider_surfaces_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-embed")

    def fake_http_request(method, url, headers, payload):
        return 401, {"error": {"message": "invalid key"}}

    provider = emb.OpenAIEmbeddingProvider(http_request=fake_http_request)
    with pytest.raises(emb.EmbeddingError, match="401"):
        provider.embed(["hello"])


def test_provider_registry_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EMBEDDING_PROVIDER", raising=False)
    assert emb.get_embedding_provider().name == "local"

    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
    assert emb.get_embedding_provider().name == "openai"

    monkeypatch.setenv("EMBEDDING_PROVIDER", "aws-bedrock")
    with pytest.raises(emb.EmbeddingConfigError, match="unknown"):
        emb.get_embedding_provider()


def _fake_provider_factory(model_name: str):
    class FakeProvider(emb.EmbeddingProvider):
        name = "fake"
        model = model_name

        def validate_config(self) -> None:
            return None

        def embed(self, texts):
            vectors = np.full((len(texts), 3), 0.1, dtype=np.float32)
            return emb._l2_normalize(vectors)

    return FakeProvider()


def test_cache_rebuilds_when_signature_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(emb, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(emb, "get_embedding_provider", lambda: _fake_provider_factory("model-a"))

    emb.build_index(
        [
            {"id": "1", "title": "One", "source": "a.md", "content": "hello world"},
            {"id": "2", "title": "Two", "source": "b.md", "content": "goodbye world"},
        ]
    )

    index_path, docs_path = emb._get_cache_paths()
    with open(docs_path, "rb") as f:
        payload = pickle.load(f)
    assert payload["signature"] == {"provider": "fake", "model": "model-a"}

    monkeypatch.setattr(emb, "get_embedding_provider", lambda: _fake_provider_factory("model-b"))
    emb._index = None
    emb._documents = None
    emb.ensure_index()

    with open(docs_path, "rb") as f:
        payload = pickle.load(f)
    assert payload["signature"] == {"provider": "fake", "model": "model-b"}
    assert payload["documents"]
    assert emb.semantic_search("hello", limit=1)


def test_semantic_search_returns_empty_on_dimension_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(emb, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(emb, "get_embedding_provider", lambda: _fake_provider_factory("dim3"))
    emb.build_index([{"id": "1", "title": "One", "source": "a.md", "content": "hello"}])

    class DimMismatchProvider(emb.EmbeddingProvider):
        name = "mismatch"
        model = "dim2"

        def validate_config(self) -> None:
            return None

        def embed(self, texts):
            vectors = np.full((len(texts), 2), 0.5, dtype=np.float32)
            return emb._l2_normalize(vectors)

    monkeypatch.setattr(emb, "get_embedding_provider", lambda: DimMismatchProvider())

    assert emb.semantic_search("anything", limit=1) == []


def test_warmup_is_off_under_pytest_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A background model load would steal CPU from the suite, so `auto` skips it."""
    monkeypatch.delenv("EMBEDDING_WARMUP", raising=False)
    monkeypatch.setattr(emb, "_running_under_pytest", lambda: True)
    assert emb.warmup_enabled() is False


def test_warmup_auto_is_on_in_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EMBEDDING_WARMUP", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setattr(emb, "_running_under_pytest", lambda: False)
    assert emb.warmup_enabled() is True


def test_warmup_auto_is_off_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMBEDDING_WARMUP", "auto")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(emb, "_running_under_pytest", lambda: False)
    assert emb.warmup_enabled() is False


@pytest.mark.parametrize("raw", ["0", "false", "no", "off"])
def test_warmup_explicit_off_overrides_everything(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("EMBEDDING_WARMUP", raw)
    monkeypatch.setattr(emb, "_running_under_pytest", lambda: False)
    assert emb.warmup_enabled() is False


@pytest.mark.parametrize("raw", ["1", "true", "yes", "on"])
def test_warmup_explicit_on_overrides_pytest_default(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("EMBEDDING_WARMUP", raw)
    monkeypatch.setattr(emb, "_running_under_pytest", lambda: True)
    assert emb.warmup_enabled() is True


def test_warm_in_background_skips_remote_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A remote provider has no local model to preload, and warming it would call the API."""
    monkeypatch.setenv("EMBEDDING_WARMUP", "1")
    monkeypatch.setattr(emb, "get_embedding_provider", lambda: _fake_provider_factory("m"))
    assert emb.warm_in_background() is False


def test_warm_in_background_starts_and_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid on-disk cache must not stop the model itself from being loaded."""
    monkeypatch.setenv("EMBEDDING_WARMUP", "1")
    provider = emb.LocalEmbeddingProvider()
    monkeypatch.setattr(emb, "get_embedding_provider", lambda: provider)

    indexed = threading.Event()
    embedded: list[list[str]] = []
    done = threading.Event()

    monkeypatch.setattr(emb, "ensure_index", lambda: indexed.set())

    def fake_embed(texts):
        embedded.append(list(texts))
        done.set()
        return np.zeros((len(texts), 3), dtype=np.float32)

    monkeypatch.setattr(provider, "embed", fake_embed)

    assert emb.warm_in_background() is True
    assert done.wait(timeout=5), "warm-up never embedded, so the model stays cold"
    assert indexed.is_set()
    assert embedded == [["warmup"]]


def test_warm_in_background_swallows_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replica must still serve when the knowledge base cannot be loaded."""
    monkeypatch.setenv("EMBEDDING_WARMUP", "1")
    monkeypatch.setattr(emb, "get_embedding_provider", lambda: emb.LocalEmbeddingProvider())

    def boom() -> None:
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(emb, "ensure_index", boom)

    logged = threading.Event()
    warnings: list[str] = []

    class _Logger:
        def warning(self, msg, *args):
            warnings.append(msg % args if args else msg)
            logged.set()

        def info(self, msg, *args):
            pass

    assert emb.warm_in_background(_Logger()) is True
    assert logged.wait(timeout=5), "warm-up failure was never logged"
    assert any("model unavailable" in w for w in warnings)


def test_warm_in_background_skipped_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMBEDDING_WARMUP", "off")
    assert emb.warm_in_background() is False