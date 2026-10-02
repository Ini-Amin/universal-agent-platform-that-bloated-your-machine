"""Minimal OpenAI-compatible chat client over httpx (Master section 33).

Base URL and key from env:
  UAP_LLM_BASE_URL  (default ``http://127.0.0.1:20128``)
  UAP_LLM_API_KEY   (fallback ``ANTHROPIC_AUTH_TOKEN``)

The key is NEVER logged, printed, or included in repr/str.
No retries — RecoveryPolicy owns that; raise on any error.
"""

from __future__ import annotations

import os
import time

import httpx

from uap.contracts import TokenUsage

__all__ = ["ChatClient", "LLMClientError"]

_DEFAULT_BASE_URL = "http://127.0.0.1:20128"


def _is_local_endpoint(url: str) -> bool:
    """True when ``url`` points at this machine (localhost/127.0.0.0/8/::1).

    Used to decide whether the operator's own ANTHROPIC_AUTH_TOKEN may be
    attached — it must never travel to a remote host implicitly.
    """
    from urllib.parse import urlparse

    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if host in {"localhost", "::1", "0.0.0.0"}:
        return True
    parts = host.split(".")
    if len(parts) == 4 and parts[0] == "127":
        return all(part.isdigit() and 0 <= int(part) <= 255 for part in parts)
    return False


class LLMClientError(Exception):
    """Any LLM API error: HTTP failure, malformed response, timeout."""


class ChatClient:
    """Thin async wrapper around ``POST /v1/chat/completions``."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        self._base_url = (
            base_url
            or os.environ.get("UAP_LLM_BASE_URL")
            or _DEFAULT_BASE_URL
        )
        explicit_key = api_key or os.environ.get("UAP_LLM_API_KEY")
        if explicit_key:
            self._api_key = explicit_key
        else:
            # SECURITY: ANTHROPIC_AUTH_TOKEN is the operator's own credential.
            # Only ever send it to a LOCAL endpoint — falling back to it for an
            # arbitrary UAP_LLM_BASE_URL would leak the token to whatever host
            # that variable points at (audit finding 2026-10-02).
            if _is_local_endpoint(self._base_url):
                self._api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or ""
            else:
                self._api_key = ""
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout_s,
            headers={"Authorization": f"Bearer {self._api_key}"},
        )

    async def complete(
        self,
        model_id: str,
        messages: list[dict],
        *,
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> tuple[str, TokenUsage]:
        """Send a chat completion request; return (text, usage)."""
        payload = {
            "model": model_id,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        start = time.perf_counter()
        try:
            resp = await self._client.post("/v1/chat/completions", json=payload)
        except httpx.TimeoutException as exc:
            raise LLMClientError(f"request timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise LLMClientError(f"HTTP error: {exc}") from exc

        latency_ms = (time.perf_counter() - start) * 1000.0

        if resp.status_code != 200:
            raise LLMClientError(
                f"HTTP {resp.status_code}: {resp.text[:300]}"
            )

        try:
            body = resp.json()
            text = body["choices"][0]["message"]["content"]
            usage = body.get("usage", {})
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMClientError(f"malformed response: {exc}") from exc

        return text, TokenUsage(
            tokens_in=int(usage.get("prompt_tokens", 0)),
            tokens_out=int(usage.get("completion_tokens", 0)),
            latency_ms=latency_ms,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def __repr__(self) -> str:
        return f"ChatClient(base_url={self._base_url!r})"

    def __str__(self) -> str:
        return repr(self)
