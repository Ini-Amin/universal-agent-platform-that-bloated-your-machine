"""Model catalog: provider-qualified model metadata (Master section 33).

The catalog is *data*. It never talks to a provider, never loads credentials
and never performs routing - it only answers "what models exist and what are
they good for". The :class:`~uap.models.router.ModelRouter` consumes it.

Provider-agnostic by construction: model ids are opaque, provider-qualified
strings (``provider/model``) and capabilities are an open enum, so a
new provider is added by registering data, not by editing routing logic.
"""

from __future__ import annotations

import os

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ModelCapability", "ModelInfo", "ModelCatalog"]


class ModelCapability(StrEnum):
    """What a model is good at (Master section 33 capability requirements)."""

    FAST = "fast"
    REASONING = "reasoning"
    CODING = "coding"
    VISION = "vision"
    EMBEDDING = "embedding"


class ModelInfo(BaseModel):
    """Immutable metadata for one provider-qualified model.

    ``extra="forbid"`` keeps the shape closed so typos in provider configs
    surface immediately instead of silently degrading routing.
    """

    model_config = ConfigDict(extra="forbid")

    id: str  # provider-qualified, e.g. "openai/gpt-4o-mini"
    provider: str
    capabilities: list[ModelCapability] = Field(default_factory=list)
    context_window: int
    cost_per_1k_in: float = 0.0
    cost_per_1k_out: float = 0.0
    latency_class: str = "standard"  # fast|standard|slow
    available: bool = True


class ModelCatalog:
    """Insertion-independent registry of :class:`ModelInfo`.

    Lookups and capability queries are deterministic: ``by_capability`` returns
    cheapest-first, ties broken by model id, so two catalogs holding the same
    models always yield the same order.
    """

    def __init__(self, models: list[ModelInfo] | None = None) -> None:
        self._models: dict[str, ModelInfo] = {}
        for info in models or []:
            self.register(info)

    def register(self, info: ModelInfo) -> None:
        """Add ``info``; a duplicate id is a configuration error."""
        if info.id in self._models:
            raise ValueError(f"model already registered: {info.id}")
        self._models[info.id] = info

    def get(self, model_id: str) -> ModelInfo | None:
        """Return the model with ``model_id``, or None when unknown."""
        return self._models.get(model_id)

    def by_capability(self, capability: ModelCapability) -> list[ModelInfo]:
        """Models declaring ``capability``, ordered cost asc then id asc."""
        return sorted(
            (m for m in self._models.values() if capability in m.capabilities),
            key=lambda m: (m.cost_per_1k_in, m.cost_per_1k_out, m.id),
        )

    def all(self) -> list[ModelInfo]:
        """Every registered model, ordered by id (deterministic snapshot)."""
        return [self._models[mid] for mid in sorted(self._models)]

    @classmethod
    def default(cls) -> "ModelCatalog":
        """The built-in starter catalog.

        Model ids are ``provider/model`` strings; which providers exist depends
        on the operator's gateway (OpenAI-compatible endpoints expose whatever
        they proxy). Override with ``UAP_MODEL_CATALOG`` (comma-separated ids)
        or construct a :class:`ModelCatalog` directly.

        Costs are 0.0 because these are reached through a local gateway; the
        cost machinery is exercised with synthetic catalogs in tests. Context
        windows are approximate, not vendor spec sheets.
        """
        override = os.environ.get("UAP_MODEL_CATALOG", "").strip()
        if override:
            ids = [item.strip() for item in override.split(",") if item.strip()]
            return cls(
                [
                    ModelInfo(
                        id=model_id,
                        provider=model_id.split("/", 1)[0] if "/" in model_id else "local",
                        capabilities=[
                            ModelCapability.REASONING,
                            ModelCapability.CODING,
                            ModelCapability.FAST,
                        ],
                        context_window=128_000,  # conservative default
                        latency_class="unknown",
                    )
                    for model_id in ids
                ]
            )
        return cls(
            [
                ModelInfo(
                    id="gpt-5.6-sol",
                    provider="local",
                    capabilities=[ModelCapability.REASONING, ModelCapability.CODING],
                    context_window=200_000,  # approximate
                    latency_class="slow",
                ),
                ModelInfo(
                    id="deepseek-v4.1-flash",
                    provider="local",
                    capabilities=[ModelCapability.FAST, ModelCapability.REASONING],
                    context_window=128_000,  # approximate
                    latency_class="fast",
                ),
                ModelInfo(
                    id="gemini-3.8-flash-high",
                    provider="local",
                    capabilities=[
                        ModelCapability.FAST,
                        ModelCapability.VISION,
                        ModelCapability.REASONING,
                    ],
                    context_window=1_000_000,  # approximate
                    latency_class="fast",
                ),
                ModelInfo(
                    id="local-reasoning-preview",
                    provider="local",
                    capabilities=[ModelCapability.REASONING, ModelCapability.CODING],
                    context_window=128_000,  # approximate
                    latency_class="standard",
                ),
                ModelInfo(
                    id="local-fast-preview",
                    provider="local",
                    capabilities=[ModelCapability.REASONING, ModelCapability.CODING],
                    context_window=128_000,  # approximate
                    latency_class="standard",
                ),
            ]
        )
