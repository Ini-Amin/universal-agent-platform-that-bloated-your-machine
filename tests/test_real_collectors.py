"""Tests for real collectors, WebClient, and capability resolution (Step 8+).

All network calls are strictly mocked with httpx.MockTransport.
Tests verify:
  1. WebClient.search parses exa response shape correctly.
  2. WebClient.search raises WebClientError on HTTP 500, malformed JSON, timeout.
  3. web_collector maps results -> Evidence with correct url/source/claim/confidence.
  4. docs_collector calls search then fetch, skipping non-http URLs.
  5. Fully-real run (no stubs) -> report has NO SIMULATION banner.
  6. Mixed run (real web + stub papers) -> report HAS SIMULATION banner naming real sources.
  7. 9router/gateway unreachable -> falls back to stubs AND banner is present.
  8. collectors_used appears in output / report artifact and names providers.
  9. MCP search tool registered in ToolRegistry is used (portable path).
  10. Provider resolution order: MCP beats HTTP; HTTP beats Stub.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from uap.contracts.models import Domain, TaskSpec, WorkflowStatus
from uap.models.web import WebClient, WebClientError
from uap.tools.policy import ToolPolicy
from uap.tools.registry import ToolRegistry, ToolSpec
from uap.workflows.research import (
    CollectorResults,
    ResearchWorkflow,
    _confidence_from_rank,
    docs_collector,
    stub_papers_collector,
    stub_web_collector,
    web_collector,
)

QUESTION = "What is the Python programming language"


def make_task(question: str = QUESTION) -> TaskSpec:
    return TaskSpec(
        domain=Domain.RESEARCH,
        goal=f"research: {question}",
        input={"question": question},
    )


# --------------------------------------------------------------------------- #
# 1. WebClient.search parses Exa response shape
# --------------------------------------------------------------------------- #


async def test_web_client_search_parses_exa_response():
    sample_response = {
        "provider": "exa",
        "query": "Python",
        "results": [
            {
                "title": "Welcome to Python.org",
                "url": "https://www.python.org",
                "snippet": "Python is a popular language.",
                "position": 1,
            },
            {
                "title": "Python Wikipedia",
                "url": "https://en.wikipedia.org/wiki/Python",
                "snippet": "Python is high-level.",
                "position": 2,
            },
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        assert data["provider"] == "exa"
        assert data["query"] == "Python"
        assert data["maxResults"] == 5
        return httpx.Response(200, json=sample_response)

    client = WebClient(
        search_url="http://mock-gateway/v1/search",
        transport=httpx.MockTransport(handler),
    )
    results = await client.search("Python", max_results=5)
    await client.aclose()

    assert len(results) == 2
    assert results[0]["title"] == "Welcome to Python.org"
    assert results[0]["url"] == "https://www.python.org"


# --------------------------------------------------------------------------- #
# 2. WebClient error handling: HTTP 500, malformed JSON, timeout, unconfigured
# --------------------------------------------------------------------------- #


async def test_web_client_search_raises_on_errors():
    # HTTP 500
    def handler_500(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")

    c_500 = WebClient(
        search_url="http://mock-gateway/v1/search",
        transport=httpx.MockTransport(handler_500),
    )
    with pytest.raises(WebClientError, match="HTTP 500"):
        await c_500.search("test")
    await c_500.aclose()

    # Malformed JSON
    def handler_malformed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not valid json {{{")

    c_malformed = WebClient(
        search_url="http://mock-gateway/v1/search",
        transport=httpx.MockTransport(handler_malformed),
    )
    with pytest.raises(WebClientError, match="malformed search JSON"):
        await c_malformed.search("test")
    await c_malformed.aclose()

    # Timeout
    def handler_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Search timed out")

    c_timeout = WebClient(
        search_url="http://mock-gateway/v1/search",
        transport=httpx.MockTransport(handler_timeout),
    )
    with pytest.raises(WebClientError, match="timed out"):
        await c_timeout.search("test")
    await c_timeout.aclose()

    # Unconfigured
    c_unconfigured = WebClient()
    assert not c_unconfigured.is_search_configured
    with pytest.raises(WebClientError, match="not configured"):
        await c_unconfigured.search("test")
    assert await c_unconfigured.available() is False


# --------------------------------------------------------------------------- #
# 3. web_collector maps results -> Evidence
# --------------------------------------------------------------------------- #


async def test_web_collector_maps_results_to_evidence():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "provider": "exa",
                "results": [
                    {
                        "title": "Python Docs",
                        "url": "https://python.org/docs",
                        "snippet": "Official docs",
                    },
                    {
                        "title": "TutorialsPoint Python",
                        "url": "https://tutorialspoint.com/python",
                        "snippet": "Easy guide",
                    },
                ],
            },
        )

    client = WebClient(
        search_url="http://mock/search",
        transport=httpx.MockTransport(handler),
    )
    evidence = await web_collector("Python", web_client=client)
    await client.aclose()

    assert len(evidence) == 2
    assert evidence[0]["source"] == "web"
    assert evidence[0]["url"] == "https://python.org/docs"
    assert evidence[0]["claim"] == "Python Docs"
    assert evidence[0]["confidence"] == _confidence_from_rank(0)
    assert evidence[1]["confidence"] == _confidence_from_rank(1)
    assert getattr(evidence, "provider", None) == "http:exa"


# --------------------------------------------------------------------------- #
# 4. docs_collector calls search then fetch, skipping non-http URLs
# --------------------------------------------------------------------------- #


async def test_docs_collector_calls_search_and_fetch_skipping_non_http():
    fetched_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "search" in url_str:
            return httpx.Response(
                200,
                json={
                    "provider": "exa",
                    "results": [
                        {"title": "FTP mirror", "url": "ftp://mirror.example.com/python"},
                        {"title": "Python Language Guide", "url": "https://docs.python.org/3/"},
                    ],
                },
            )
        if "fetch" in url_str:
            body = json.loads(request.content)
            fetched_urls.append(body["url"])
            return httpx.Response(
                200,
                json={
                    "provider": "firecrawl",
                    "url": body["url"],
                    "title": "Python 3 Documentation",
                    "content": {
                        "format": "markdown",
                        "text": "# Welcome\n\nPython is an interpreted high-level language.",
                    },
                },
            )
        return httpx.Response(404)

    client = WebClient(
        search_url="http://mock/v1/search",
        fetch_url="http://mock/v1/web/fetch",
        transport=httpx.MockTransport(handler),
    )
    evidence = await docs_collector("Python", web_client=client)
    await client.aclose()

    # FTP url was SKIPPED
    assert "ftp://mirror.example.com/python" not in fetched_urls
    # HTTP url was fetched
    assert "https://docs.python.org/3/" in fetched_urls
    assert len(evidence) >= 1
    assert evidence[0]["source"] == "docs"
    assert evidence[0]["url"] == "https://docs.python.org/3/"
    assert "Python" in evidence[0]["claim"]


# --------------------------------------------------------------------------- #
# 5. Fully-real run -> NO SIMULATION banner
# --------------------------------------------------------------------------- #


async def test_fully_real_run_has_no_simulation_banner():
    async def real_web(question: str):
        return CollectorResults(
            [
                {
                    "source": "web",
                    "claim": "Python is dynamic",
                    "url": "https://real-web.org/python",
                    "confidence": 0.95,
                }
            ],
            provider="http:exa",
        )

    async def real_docs(question: str):
        return CollectorResults(
            [
                {
                    "source": "docs",
                    "claim": "Python is dynamic",
                    "url": "https://real-docs.org/python",
                    "confidence": 0.90,
                }
            ],
            provider="http:firecrawl",
        )

    workflow = ResearchWorkflow(collectors=[real_web, real_docs])
    assert workflow.simulated is False

    result = await workflow.run(make_task())

    assert result.status == WorkflowStatus.COMPLETED
    assert workflow.simulated is False
    assert "> **SIMULATION — DETERMINISTIC STUB EVIDENCE**" not in result.output
    # Real URLs are present
    assert "https://real-web.org/python" in result.output
    assert "https://real-docs.org/python" in result.output


# --------------------------------------------------------------------------- #
# 6. Mixed run -> HAS SIMULATION banner naming real sources
# --------------------------------------------------------------------------- #


async def test_mixed_run_has_simulation_banner_and_names_real_sources():
    async def real_web(question: str):
        return CollectorResults(
            [
                {
                    "source": "web",
                    "claim": "Python is dynamic",
                    "url": "https://real-web.org/python",
                    "confidence": 0.95,
                }
            ],
            provider="http:exa",
        )

    # 1 real (real_web) + 1 stub (stub_papers_collector)
    workflow = ResearchWorkflow(collectors=[real_web, stub_papers_collector])
    result = await workflow.run(make_task())

    assert result.status == WorkflowStatus.COMPLETED
    assert workflow.simulated is True
    # Simulation banner is present
    assert "> **SIMULATION — DETERMINISTIC STUB EVIDENCE**" in result.output
    # Real source active is named
    assert "(Real sources active: http:exa)" in result.output


# --------------------------------------------------------------------------- #
# 7. Gateway unreachable -> falls back to stubs AND banner is present
# --------------------------------------------------------------------------- #


async def test_unreachable_endpoint_falls_back_to_stubs_with_banner():
    def failing_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="Bad Gateway")

    failing_client = WebClient(
        search_url="http://failing-gateway/v1/search",
        fetch_url="http://failing-gateway/v1/web/fetch",
        transport=httpx.MockTransport(failing_handler),
    )

    workflow = ResearchWorkflow(web_client=failing_client)
    result = await workflow.run(make_task())
    await failing_client.aclose()

    assert result.status == WorkflowStatus.COMPLETED
    assert workflow.simulated is True
    assert "> **SIMULATION — DETERMINISTIC STUB EVIDENCE**" in result.output
    # Evidence contains stub URLs
    assert "https://example.com/python" in result.output


# --------------------------------------------------------------------------- #
# 8. collectors_used appears in output and report artifact
# --------------------------------------------------------------------------- #


async def test_collectors_used_in_output_and_report_artifact():
    async def real_web(question: str):
        return CollectorResults(
            [
                {
                    "source": "web",
                    "claim": "Python language",
                    "url": "https://real.python.org",
                    "confidence": 0.9,
                }
            ],
            provider="http:exa",
        )

    async def real_docs(question: str):
        return CollectorResults(
            [
                {
                    "source": "docs",
                    "claim": "Python language",
                    "url": "https://docs.python.org",
                    "confidence": 0.85,
                }
            ],
            provider="http:firecrawl",
        )

    workflow = ResearchWorkflow(collectors=[real_web, real_docs])
    result = await workflow.run(make_task())

    assert result.status == WorkflowStatus.COMPLETED
    # collectors_used in output
    assert "- collectors_used: http:exa, http:firecrawl" in result.output

    # collectors_used in report.md artifact
    report_artifact = next(a for a in result.artifacts if a.type == "report.md")
    assert "- collectors_used: http:exa, http:firecrawl" in str(report_artifact.content_ref)


# --------------------------------------------------------------------------- #
# 9. Fake MCP search tool registered in ToolRegistry is used (portable path)
# --------------------------------------------------------------------------- #


async def test_mcp_search_tool_used_when_registered():
    registry = ToolRegistry()
    mcp_called = False

    async def fake_search(query: str, **kwargs):
        nonlocal mcp_called
        mcp_called = True
        return [
            {
                "title": "MCP Python Guide",
                "url": "https://mcp.python.guide",
                "snippet": "Python through MCP tool",
            }
        ]

    registry.register(
        ToolSpec(name="exa.search", description="Exa MCP Search", risk_tier=0),
        fake_search,
    )

    evidence = await web_collector("Python", tools=registry)
    assert mcp_called is True
    assert len(evidence) == 1
    assert evidence[0]["url"] == "https://mcp.python.guide"
    assert evidence[0]["provider"] == "mcp:exa.search"
    assert getattr(evidence, "provider", None) == "mcp:exa.search"


# --------------------------------------------------------------------------- #
# 10. Provider resolution order: MCP beats HTTP beats Stub
# --------------------------------------------------------------------------- #


async def test_provider_resolution_order():
    # Case A: Both MCP and HTTP configured -> MCP must win, HTTP is NOT called
    http_called = False

    def http_handler(request: httpx.Request) -> httpx.Response:
        nonlocal http_called
        http_called = True
        return httpx.Response(200, json={"provider": "exa", "results": []})

    http_client = WebClient(
        search_url="http://mock/search",
        transport=httpx.MockTransport(http_handler),
    )
    mcp_called = False
    registry = ToolRegistry()
    async def fake_mcp_search(query: str, **kwargs):
        nonlocal mcp_called
        mcp_called = True
        return [{"title": "MCP won", "url": "https://mcp-won.org"}]

    registry.register(
        ToolSpec(name="web_search", description="MCP Search", risk_tier=0),
        fake_mcp_search,
    )

    evidence = await web_collector("test", web_client=http_client, tools=registry)
    assert mcp_called is True
    assert http_called is False  # MCP beat HTTP
    assert getattr(evidence, "provider", None) == "mcp:web_search"
    await http_client.aclose()

    # Case B: No MCP tool, HTTP client configured -> HTTP wins
    http_called_b = False

    def http_handler_b(request: httpx.Request) -> httpx.Response:
        nonlocal http_called_b
        http_called_b = True
        return httpx.Response(
            200,
            json={
                "provider": "exa",
                "results": [{"title": "HTTP won", "url": "https://http-won.org"}],
            },
        )

    http_client_b = WebClient(
        search_url="http://mock/search",
        transport=httpx.MockTransport(http_handler_b),
    )
    evidence_b = await web_collector("test", web_client=http_client_b, tools=None)
    assert http_called_b is True
    assert getattr(evidence_b, "provider", None) == "http:exa"
    await http_client_b.aclose()

    # Case C: Neither MCP nor HTTP -> Stub wins
    evidence_c = await web_collector("test", web_client=None, tools=None)
    assert getattr(evidence_c, "provider", None) == "stub:web"
    assert evidence_c[0]["url"] == "https://example.com/python"
