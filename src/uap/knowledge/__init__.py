"""Knowledge layer: curated, provenance-backed, searchable claims.

Master sections 13-15 keep Knowledge separate from Memory
(:mod:`uap.memory`, the old SQLite store) and from Artifacts
(:mod:`uap.artifacts`). This package is the new, PostgreSQL/pgvector-backed
knowledge store.

Public surface:

* :class:`KnowledgeItem`, :class:`Provenance`, :class:`KnowledgeStatus` - the
  domain models.
* :class:`HashingEmbedder`, :class:`Embedder`, :func:`embed_texts` -
  deterministic, network-free embeddings.
* :class:`KnowledgeStore` - persistence over an injected SQLAlchemy session.
* :class:`KnowledgeLifecycle`, :class:`PromotionPolicy`,
  :class:`PolicyDeniedError` - policy-gated promotion (section 15).
"""

from __future__ import annotations

from uap.knowledge.api_embedder import (
    EmbedderError,
    OllamaEmbedder,
)
from uap.knowledge.embeddings import (
    EMBED_AUTO_ENV,
    EMBED_BACKEND_ENV,
    EMBEDDING_DIM,
    Embedder,
    HashingEmbedder,
    embed_texts,
    get_embedder,
)
from uap.knowledge.lifecycle import (
    KnowledgeLifecycle,
    PolicyDeniedError,
    PromotionPolicy,
)
from uap.knowledge.model import (
    SOURCE_KINDS,
    KnowledgeItem,
    KnowledgeStatus,
    Provenance,
)
from uap.knowledge.store import KnowledgeStore

__all__ = [
    "EMBED_AUTO_ENV",
    "EMBED_BACKEND_ENV",
    "EMBEDDING_DIM",
    "Embedder",
    "EmbedderError",
    "HashingEmbedder",
    "OllamaEmbedder",
    "KnowledgeItem",
    "KnowledgeLifecycle",
    "KnowledgeStatus",
    "KnowledgeStore",
    "PolicyDeniedError",
    "PromotionPolicy",
    "Provenance",
    "embed_texts",
    "get_embedder",
]
