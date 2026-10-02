"""API-backed embeddings via a local or remote Ollama server.

Provides real dense vector embeddings using Ollama (default http://127.0.0.1:11434).
nomic-embed-text natively returns 768 dims; truncated to EMBEDDING_DIM=384
(Matryoshka representation - truncation preserves the nearest-neighbor ordering).
"""

from __future__ import annotations

import httpx

from uap.knowledge.embeddings import EMBEDDING_DIM


class EmbedderError(Exception):
    """Raised when an embedder fails to generate embeddings."""


class OllamaEmbedder:
    """Real embeddings via a local ollama server (default http://127.0.0.1:11434).

    nomic-embed-text natively returns 768 dims; truncated to EMBEDDING_DIM=384
    (Matryoshka representation — truncation preserves the nearest-neighbor ordering).
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "nomic-embed-text",
        timeout_s: float = 30.0,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self._client = client

    def _get_client(self) -> tuple[httpx.Client, bool]:
        if self._client is not None:
            return self._client, False
        return httpx.Client(timeout=self.timeout_s), True

    def embed(self, text: str) -> list[float]:
        """POST /api/embeddings; raise EmbedderError on failure.

        Takes the first 384 dimensions. If the returned vector has fewer than
        384 dimensions, it is zero-padded up to EMBEDDING_DIM.
        """
        payload = {"model": self.model, "prompt": text}
        client, should_close = self._get_client()
        try:
            resp = client.post(f"{self.base_url}/api/embeddings", json=payload)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as err:
            raise EmbedderError(
                f"Ollama HTTP error {err.response.status_code}: {err.response.text}"
            ) from err
        except httpx.RequestError as err:
            raise EmbedderError(f"Ollama connection error: {err}") from err
        except Exception as err:
            raise EmbedderError(f"Failed to get embeddings from Ollama: {err}") from err
        finally:
            if should_close:
                client.close()

        embedding = data.get("embedding")
        if not isinstance(embedding, list) or not embedding:
            raise EmbedderError(f"Unexpected response from Ollama embeddings: {data}")

        # Truncate to EMBEDDING_DIM (Matryoshka) or zero-pad if fewer
        if len(embedding) >= EMBEDDING_DIM:
            return [float(x) for x in embedding[:EMBEDDING_DIM]]
        return [float(x) for x in embedding] + [0.0] * (EMBEDDING_DIM - len(embedding))

    def available(self) -> bool:
        """GET /api/tags; True when the model is present."""
        client, should_close = self._get_client()
        try:
            resp = client.get(f"{self.base_url}/api/tags")
            if resp.status_code != 200:
                return False
            data = resp.json()
            models = data.get("models", [])
            for m in models:
                name = m.get("name", "")
                model_attr = m.get("model", "")
                target = self.model
                if (
                    name == target
                    or name.startswith(f"{target}:")
                    or model_attr == target
                    or model_attr.startswith(f"{target}:")
                ):
                    return True
            return False
        except Exception:
            return False
        finally:
            if should_close:
                client.close()
