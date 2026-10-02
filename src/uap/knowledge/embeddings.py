"""Deterministic, network-free embeddings (Master sections 13, 25, 41).

The knowledge layer must be searchable **without any network access** and with
bit-for-bit reproducible results, so this module ships a hashing embedder
instead of calling a model provider. Master section 13 requires provider
abstraction: :class:`Embedder` is the seam - a real model embedder can be
injected later behind the same protocol, provided it produces vectors of
:data:`EMBEDDING_DIM` dimensions (the stored ``vector(384)`` column width is
fixed by :class:`HashingEmbedder`).

Algorithm (``HashingEmbedder``)
-------------------------------

1. Tokenize ``text.lower()`` with the alphanumeric regex ``[a-z0-9]+`` (so
   punctuation and whitespace are separators; Unicode letters are dropped by
   this ASCII tokenizer).
2. For each token, compute ``index = int.from_bytes(sha256(token).digest(),
   "big") % EMBEDDING_DIM`` and add ``1.0`` to that bucket (a bag-of-words
   count sketch).
3. L2-normalize the resulting ``EMBEDDING_DIM``-vector (a zero vector is
   returned unchanged).

Determinism: ``sha256`` is used - **never** Python's ``hash()`` - so the output
is independent of ``PYTHONHASHSEED`` and identical across processes and runs.

Cosine distance in PostgreSQL is then ``embedding <=> :query_vector`` and the
ANN index is an ``ivfflat`` index using ``vector_cosine_ops``.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Protocol, runtime_checkable

__all__ = [
    "EMBEDDING_DIM",
    "Embedder",
    "HashingEmbedder",
    "EMBED_AUTO_ENV",
    "EMBED_BACKEND_ENV",
    "embed_texts",
    "get_embedder",
]

#: Fixed vector width. The ``knowledge_items.embedding`` column is
#: ``vector(384)``; a replacement embedder must emit exactly this many floats.
EMBEDDING_DIM = 384

#: Lowercase alphanumeric tokens (bag-of-words). ASCII-only by design.
_TOKEN_RE = re.compile(r"[a-z0-9]+")

@runtime_checkable
class Embedder(Protocol):
    """Anything that turns text into a fixed-width dense vector."""

    def embed(self, text: str) -> list[float]:
        """Return an :data:`EMBEDDING_DIM`-length dense vector for ``text``."""
        ...

class HashingEmbedder:
    """Deterministic bag-of-words hashing embedder (see module docstring).

    Fully local, no network, no model files, and stable across processes
    because it hashes with ``sha256`` rather than Python's salted ``hash()``.
    """

    #: Vector width produced by this embedder (== the stored column width).
    dim: int = EMBEDDING_DIM

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * EMBEDDING_DIM
        for token in _TOKEN_RE.findall(text.lower()):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest, "big") % EMBEDDING_DIM
            vector[index] += 1.0

        norm = math.sqrt(sum(value * value for value in vector))
        if norm > 0.0:
            vector = [value / norm for value in vector]
        return vector

def embed_texts(embedder: Embedder, texts: list[str]) -> list[list[float]]:
    """Batch helper: embed ``texts`` with ``embedder`` in order."""

    return [embedder.embed(text) for text in texts]

EMBED_BACKEND_ENV = "UAP_EMBED_BACKEND"
EMBED_AUTO_ENV = "UAP_EMBED_AUTO"


def get_embedder() -> Embedder:
    """Factory: HashingEmbedder by default (deterministic, test-safe);
    OllamaEmbedder when UAP_EMBED_BACKEND=ollama (or when ollama is available and UAP_EMBED_AUTO=1).
    """
    backend = os.environ.get(EMBED_BACKEND_ENV, "").strip().lower()
    if backend == "ollama":
        from uap.knowledge.api_embedder import OllamaEmbedder

        return OllamaEmbedder()

    auto = os.environ.get(EMBED_AUTO_ENV, "").strip().lower()
    if auto in ("1", "true", "yes"):
        try:
            from uap.knowledge.api_embedder import OllamaEmbedder

            embedder = OllamaEmbedder()
            if embedder.available():
                return embedder
        except Exception:
            pass

    return HashingEmbedder()
