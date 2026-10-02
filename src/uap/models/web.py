"""HTTP web client for search and fetch capabilities (Master section 7).

Optional HTTP provider used when explicitly configured via environment:
  UAP_SEARCH_URL      (e.g. ``http://127.0.0.1:20128/v1/search``)
  UAP_FETCH_URL       (e.g. ``http://127.0.0.1:20128/v1/web/fetch``)
  UAP_WEB_BASE_URL    (convenience base url: /v1/search and /v1/web/fetch)
  UAP_SEARCH_PROVIDER (default ``"exa"``)
  UAP_FETCH_PROVIDER  (default ``"firecrawl"``)
  UAP_LLM_API_KEY / UAP_WEB_API_KEY (fallback ``ANTHROPIC_AUTH_TOKEN`` for local endpoints)

When URLs are unset, WebClient is unconfigured (is_configured=False) and collectors
fall back through the provider resolution order (MCP -> HTTP -> Stub).
The API key is NEVER logged, printed, or included in repr/str.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from uap.models.client import _is_local_endpoint

__all__ = ["WebClient", "WebClientError"]


class WebClientError(Exception):
    """Any Web API error: HTTP failure, malformed response, timeout, unreachable."""


class WebClient:
    """Thin async wrapper around search and fetch HTTP endpoints."""

    def __init__(
        self,
        base_url: str | None = None,
        search_url: str | None = None,
        fetch_url: str | None = None,
        api_key: str | None = None,
        search_provider: str | None = None,
        fetch_provider: str | None = None,
        timeout_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        base = base_url or os.environ.get("UAP_WEB_BASE_URL")
        self._search_url = (
            search_url
            or os.environ.get("UAP_SEARCH_URL")
            or (f"{base.rstrip('/')}/v1/search" if base else None)
        )
        self._fetch_url = (
            fetch_url
            or os.environ.get("UAP_FETCH_URL")
            or (f"{base.rstrip('/')}/v1/web/fetch" if base else None)
        )
        self.search_provider = (
            search_provider
            or os.environ.get("UAP_SEARCH_PROVIDER")
            or "exa"
        )
        self.fetch_provider = (
            fetch_provider
            or os.environ.get("UAP_FETCH_PROVIDER")
            or "firecrawl"
        )
        self._timeout_s = timeout_s

        explicit_key = api_key or os.environ.get("UAP_LLM_API_KEY") or os.environ.get("UAP_WEB_API_KEY")
        if explicit_key:
            self._api_key = explicit_key
        else:
            # SECURITY: ANTHROPIC_AUTH_TOKEN is only sent to local endpoints.
            target = self._search_url or self._fetch_url or base or ""
            if target and _is_local_endpoint(target):
                self._api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or ""
            else:
                self._api_key = ""

        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=timeout_s,
            transport=transport,
        )

    @property
    def is_configured(self) -> bool:
        """True if at least one endpoint URL is configured."""
        return bool(self._search_url or self._fetch_url)

    @property
    def is_search_configured(self) -> bool:
        return bool(self._search_url)

    @property
    def is_fetch_configured(self) -> bool:
        return bool(self._fetch_url)

    async def search(self, query: str, max_results: int = 5) -> list[dict[str, Any]]:
        """POST to search endpoint with provider and query; return list of result dicts."""
        if not self._search_url:
            raise WebClientError("search URL is not configured")

        payload = {
            "provider": self.search_provider,
            "query": query,
            "maxResults": max_results,
        }
        try:
            resp = await self._client.post(self._search_url, json=payload)
        except httpx.TimeoutException as exc:
            raise WebClientError(f"search request timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise WebClientError(f"search HTTP error: {exc}") from exc

        if resp.status_code != 200:
            raise WebClientError(
                f"search HTTP {resp.status_code}: {resp.text[:300]}"
            )

        try:
            body = resp.json()
        except Exception as exc:
            raise WebClientError(f"malformed search JSON response: {exc}") from exc

        if not isinstance(body, dict):
            raise WebClientError(f"unexpected search response type: {type(body)}")

        results = body.get("results")
        if not isinstance(results, list):
            raise WebClientError(f"search response missing 'results' list: {list(body.keys())}")

        return results

    async def fetch(self, url: str) -> dict[str, Any]:
        """POST to fetch endpoint with provider and url; return page content dict."""
        if not self._fetch_url:
            raise WebClientError("fetch URL is not configured")

        payload = {
            "provider": self.fetch_provider,
            "url": url,
        }
        try:
            resp = await self._client.post(self._fetch_url, json=payload)
        except httpx.TimeoutException as exc:
            raise WebClientError(f"fetch request timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise WebClientError(f"fetch HTTP error: {exc}") from exc

        if resp.status_code != 200:
            raise WebClientError(
                f"fetch HTTP {resp.status_code}: {resp.text[:300]}"
            )

        try:
            body = resp.json()
        except Exception as exc:
            raise WebClientError(f"malformed fetch JSON response: {exc}") from exc

        if not isinstance(body, dict):
            raise WebClientError(f"unexpected fetch response type: {type(body)}")

        return body

    async def available(self) -> bool:
        """Probe search availability with a minimal query.

        Returns False immediately without network I/O if search URL is unconfigured.
        Returns False on timeout, HTTP error, or status != 200.
        """
        if not self._search_url:
            return False
        try:
            payload = {
                "provider": self.search_provider,
                "query": "ping",
                "maxResults": 1,
            }
            resp = await self._client.post(
                self._search_url,
                json=payload,
                timeout=min(self._timeout_s, 3.0),
            )
            return resp.status_code == 200
        except Exception:
            return False

    async def aclose(self) -> None:
        await self._client.aclose()

    def __repr__(self) -> str:
        return (
            f"WebClient(search_url={self._search_url!r}, "
            f"fetch_url={self._fetch_url!r}, "
            f"search_provider={self.search_provider!r}, "
            f"fetch_provider={self.fetch_provider!r})"
        )

    def __str__(self) -> str:
        return repr(self)
