"""Tests for OllamaEmbedder and get_embedder() factory."""

from __future__ import annotations

import os
import httpx
import pytest

from uap.knowledge.api_embedder import EmbedderError, OllamaEmbedder
from uap.knowledge.embeddings import (
    EMBED_AUTO_ENV,
    EMBED_BACKEND_ENV,
    EMBEDDING_DIM,
    HashingEmbedder,
    get_embedder,
)


def test_get_embedder_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """1. get_embedder() with no env -> HashingEmbedder (deterministic, existing behavior preserved)."""
    monkeypatch.delenv(EMBED_BACKEND_ENV, raising=False)
    monkeypatch.delenv(EMBED_AUTO_ENV, raising=False)
    embedder = get_embedder()
    assert isinstance(embedder, HashingEmbedder)
    vec = embedder.embed("test")
    assert len(vec) == EMBEDDING_DIM


def test_get_embedder_ollama_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """2. get_embedder() with UAP_EMBED_BACKEND=ollama -> OllamaEmbedder instance."""
    monkeypatch.setenv(EMBED_BACKEND_ENV, "ollama")
    embedder = get_embedder()
    assert isinstance(embedder, OllamaEmbedder)


def test_embed_truncation() -> None:
    """3. OllamaEmbedder.embed parses a mocked 768-dim response and returns EXACTLY 384 floats."""
    fake_768 = [float(i) for i in range(768)]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/embeddings"
        return httpx.Response(200, json={"embedding": fake_768})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    embedder = OllamaEmbedder(client=client)
    res = embedder.embed("hello")
    assert len(res) == 384
    assert res == fake_768[:384]


def test_embed_padding() -> None:
    """4. Padding: a mocked 256-dim response -> 384 with zero-padding."""
    fake_256 = [float(i + 1) for i in range(256)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embedding": fake_256})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    embedder = OllamaEmbedder(client=client)
    res = embedder.embed("hello")
    assert len(res) == 384
    assert res[:256] == fake_256
    assert res[256:] == [0.0] * 128


def test_embed_raises_on_http_error() -> None:
    """5. embed raises EmbedderError on HTTP error (mocked)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    embedder = OllamaEmbedder(client=client)
    with pytest.raises(EmbedderError, match="Ollama HTTP error 500"):
        embedder.embed("hello")


def test_embed_raises_on_connection_error() -> None:
    """6. On connection refused (mocked transport)."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    embedder = OllamaEmbedder(client=client)
    with pytest.raises(EmbedderError, match="Ollama connection error"):
        embedder.embed("hello")


def test_available_tags() -> None:
    """7. available() False when the model is absent from /api/tags (mocked); True when present."""

    # Present case
    def handler_present(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [{"name": "nomic-embed-text:latest"}]})

    client_present = httpx.Client(transport=httpx.MockTransport(handler_present))
    embedder_present = OllamaEmbedder(model="nomic-embed-text", client=client_present)
    assert embedder_present.available() is True

    # Absent case
    def handler_absent(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": "llama3:latest"}]})

    client_absent = httpx.Client(transport=httpx.MockTransport(handler_absent))
    embedder_absent = OllamaEmbedder(model="nomic-embed-text", client=client_absent)
    assert embedder_absent.available() is False

    # Server error case -> returns False (never crashes)
    def handler_err(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="Bad Gateway")

    client_err = httpx.Client(transport=httpx.MockTransport(handler_err))
    embedder_err = OllamaEmbedder(client=client_err)
    assert embedder_err.available() is False


def test_embed_auto_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """8. Test UAP_EMBED_AUTO flag with available / unavailable."""
    monkeypatch.delenv(EMBED_BACKEND_ENV, raising=False)
    monkeypatch.setenv(EMBED_AUTO_ENV, "1")

    # When Ollama is available
    monkeypatch.setattr(OllamaEmbedder, "available", lambda self: True)
    embedder = get_embedder()
    assert isinstance(embedder, OllamaEmbedder)

    # When Ollama is NOT available
    monkeypatch.setattr(OllamaEmbedder, "available", lambda self: False)
    embedder_fallback = get_embedder()
    assert isinstance(embedder_fallback, HashingEmbedder)


def test_embed_invalid_response_format() -> None:
    """embed raises EmbedderError when response json does not contain embedding list."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": "unknown"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    embedder = OllamaEmbedder(client=client)
    with pytest.raises(EmbedderError, match="Unexpected response"):
        embedder.embed("test")


@pytest.mark.skipif(
    not os.environ.get("UAP_LIVE"),
    reason="Requires running Ollama server with UAP_LIVE=1",
)
def test_live_ollama_embed() -> None:
    """9. LIVE test: real embed of 'hello world' via running ollama -> len == 384, different texts produce different vectors."""
    embedder = OllamaEmbedder()
    assert embedder.available(), "Ollama model nomic-embed-text is not available"
    vec1 = embedder.embed("hello world")
    assert len(vec1) == 384
    assert all(isinstance(x, float) for x in vec1)

    vec2 = embedder.embed("completely different text about astronomy and stars")
    assert len(vec2) == 384
    assert vec1 != vec2
