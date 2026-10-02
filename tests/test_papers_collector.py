"""Tests for the real papers collector (MCP -> HTTP -> stub resolution).

All network calls are mocked with httpx.MockTransport; nothing hits the network.
  1. MCP paper tool detected by name and used.
  2. MCP paper tool preferred over a generic search tool.
  3. Generic MCP search tool used when no paper-flavoured tool exists.
  4. HTTP fallback with an academic-shaped query when no MCP tool exists.
  5. Stub fallback when neither MCP nor HTTP is available -> simulated.
  6. All-real run (web + papers + docs) -> NO SIMULATION banner.
  7. Mixed run (papers real, docs stub) -> banner present.
  8. collectors_used names the papers provider.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from uap.contracts.models import Domain, TaskSpec, WorkflowStatus
from uap.models.web import WebClient
from uap.tools.registry import ToolRegistry, ToolSpec
from uap.workflows.research import (
    ResearchWorkflow,
    find_paper_tool,
    papers_collector,
)

QUESTION = "What is the Python programming language"
SIMULATION_BANNER = "> **SIMULATION — DETERMINISTIC STUB EVIDENCE**"


def make_task(question: str = QUESTION) -> TaskSpec:
    return TaskSpec(
        domain=Domain.RESEARCH,
        goal=f"research: {question}",
        input={"question": question},
    )


def registry_with(*names: str, result: list[dict[str, Any]]) -> tuple[ToolRegistry, list[str]]:
    """Registry holding one fake tool per name; returns (registry, call log)."""
    calls: list[str] = []
    registry = ToolRegistry()
    for name in names:

        async def fake(query: str, _name: str = name, **kwargs: Any) -> list[dict[str, Any]]:
            calls.append(_name)
            return result

        registry.register(
            ToolSpec(name=name, description=f"MCP {name}", risk_tier=0),
            fake,
        )
    return registry, calls


def search_client(
    results: list[dict[str, Any]],
    *,
    queries: list[str] | None = None,
    fetch_body: dict[str, Any] | None = None,
) -> WebClient:
    """WebClient over MockTransport; records search queries."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            if queries is not None:
                queries.append(json.loads(request.content)["query"])
            return httpx.Response(200, json={"provider": "exa", "results": results})
        return httpx.Response(
            200,
            json=fetch_body if fetch_body is not None else {"title": "", "content": {"text": ""}},
        )

    return WebClient(
        search_url="http://mock/v1/search",
        fetch_url="http://mock/v1/web/fetch",
        transport=httpx.MockTransport(handler),
    )


async def test_mcp_paper_tool_detected_and_used():
    registry, calls = registry_with(
        "paper_search",
        result=[{"title": "Attention Is All You Need", "url": "https://arxiv.org/abs/1706.03762"}],
    )

    assert find_paper_tool(registry) is not None
    assert find_paper_tool(registry).name == "paper_search"

    evidence = await papers_collector("transformers", tools=registry)

    assert calls == ["paper_search"]
    assert len(evidence) == 1
    assert evidence[0]["source"] == "papers"
    assert evidence[0]["url"] == "https://arxiv.org/abs/1706.03762"
    assert evidence[0]["provider"] == "mcp:paper_search"
    assert getattr(evidence, "provider", None) == "mcp:paper_search"


async def test_mcp_paper_tool_preferred_over_generic_search_tool():
    registry, calls = registry_with(
        "web_search",
        "semantic_scholar.search",
        result=[{"title": "Paper", "url": "https://papers.example/1"}],
    )

    evidence = await papers_collector("question", tools=registry)

    assert calls == ["semantic_scholar.search"]  # not the generic web_search
    assert getattr(evidence, "provider", None) == "mcp:semantic_scholar.search"


async def test_generic_mcp_search_tool_used_when_no_paper_tool():
    registry, calls = registry_with(
        "web_search", result=[{"title": "A Paper", "url": "https://example.org/p"}]
    )

    assert find_paper_tool(registry) is None
    evidence = await papers_collector("question", tools=registry)

    assert calls == ["web_search"]
    assert evidence[0]["source"] == "papers"
    assert getattr(evidence, "provider", None) == "mcp:web_search"


# --------------------------------------------------------------------------- #
# 4. HTTP tier
# --------------------------------------------------------------------------- #


async def test_http_fallback_used_when_no_mcp_paper_tool():
    queries: list[str] = []
    client = search_client(
        [{"title": "Python paper", "url": "https://arxiv.org/abs/9001.0001"}],
        queries=queries,
    )

    evidence = await papers_collector(
        QUESTION, web_client=client, tools=registry_with("unrelated.tool", result=[])[0]
    )
    await client.aclose()

    assert queries == [f"{QUESTION} arxiv paper"]  # academic-shaped query
    assert evidence[0]["source"] == "papers"
    assert evidence[0]["url"] == "https://arxiv.org/abs/9001.0001"
    assert getattr(evidence, "provider", None) == "http:exa"


async def test_mcp_beats_http_for_papers():
    http_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal http_called
        http_called = True
        return httpx.Response(200, json={"provider": "exa", "results": []})

    client = WebClient(search_url="http://mock/search", transport=httpx.MockTransport(handler))
    registry, calls = registry_with(
        "arxiv_mcp.search", result=[{"title": "MCP paper", "url": "https://arxiv.org/abs/1"}]
    )

    evidence = await papers_collector("q", web_client=client, tools=registry)
    await client.aclose()

    assert calls == ["arxiv_mcp.search"]
    assert http_called is False
    assert getattr(evidence, "provider", None) == "mcp:arxiv_mcp.search"


# --------------------------------------------------------------------------- #
# 5. Stub tier
# --------------------------------------------------------------------------- #


async def test_stub_fallback_when_no_provider(monkeypatch):
    monkeypatch.delenv("UAP_SEARCH_URL", raising=False)
    monkeypatch.delenv("UAP_WEB_BASE_URL", raising=False)

    evidence = await papers_collector(QUESTION, web_client=None, tools=None)

    assert getattr(evidence, "provider", None) == "stub:papers"
    assert all(item["provider"] == "stub:papers" for item in evidence)
    assert evidence[0]["url"] == "https://arxiv.org/abs/0001"


async def test_stub_papers_makes_run_simulated(monkeypatch):
    monkeypatch.delenv("UAP_SEARCH_URL", raising=False)
    monkeypatch.delenv("UAP_WEB_BASE_URL", raising=False)

    workflow = ResearchWorkflow()
    assert workflow.simulated is True

    result = await workflow.run(make_task())

    assert result.status == WorkflowStatus.COMPLETED
    assert workflow.simulated is True
    assert SIMULATION_BANNER in result.output


# --------------------------------------------------------------------------- #
# 6 + 7 + 8. Banner behaviour and collectors_used
# --------------------------------------------------------------------------- #


def _all_real_client() -> WebClient:
    """Serves web, papers and docs queries plus a fetch, all over MockTransport."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            query = json.loads(request.content)["query"]
            if "arxiv paper" in query:
                return httpx.Response(
                    200,
                    json={
                        "provider": "exa",
                        "results": [
                            {"title": "Python paper", "url": "https://arxiv.org/abs/2401.00001"}
                        ],
                    },
                )
            if query.endswith("documentation"):
                return httpx.Response(
                    200,
                    json={
                        "provider": "exa",
                        "results": [{"title": "Python docs", "url": "https://docs.python.org/3/"}],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "provider": "exa",
                    "results": [{"title": "Python language", "url": "https://www.python.org/"}],
                },
            )
        return httpx.Response(
            200,
            json={"title": "Python docs", "content": {"text": "Python documentation body."}},
        )

    return WebClient(
        search_url="http://mock/v1/search",
        fetch_url="http://mock/v1/web/fetch",
        transport=httpx.MockTransport(handler),
    )


async def test_all_real_collectors_produce_no_simulation_banner():
    client = _all_real_client()
    workflow = ResearchWorkflow(web_client=client)

    assert workflow.simulated is False
    assert [getattr(c, "__name__", "") for c in workflow.collectors] == [
        "web_collector",
        "papers_collector",
        "docs_collector",
    ]

    result = await workflow.run(make_task())

    assert result.status == WorkflowStatus.COMPLETED
    assert workflow.simulated is False
    assert SIMULATION_BANNER not in result.output
    assert "arxiv.org/abs/000" not in result.output
    assert "https://www.python.org/" in result.output
    assert "https://arxiv.org/abs/2401.00001" in result.output
    assert "https://docs.python.org/3/" in result.output

    await client.aclose()


async def test_mixed_run_papers_real_docs_stub_has_banner():
    """Only the papers query returns hits, so web/docs fall back to their stubs."""

    def handler(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        results = (
            [{"title": "Python paper", "url": "https://arxiv.org/abs/2401.00003"}]
            if "arxiv paper" in query
            else []
        )
        return httpx.Response(200, json={"provider": "exa", "results": results})

    client = WebClient(search_url="http://mock/v1/search", transport=httpx.MockTransport(handler))
    workflow = ResearchWorkflow(web_client=client)

    result = await workflow.run(make_task())

    assert result.status == WorkflowStatus.COMPLETED
    assert workflow.simulated is True
    assert SIMULATION_BANNER in result.output
    assert "https://arxiv.org/abs/2401.00003" in result.output
    collectors_line = next(
        line for line in result.output.splitlines() if line.startswith("- collectors_used:")
    )
    assert "http:exa" in collectors_line  # papers went real
    assert "stub:docs" in collectors_line  # docs stayed a stub

    await client.aclose()


async def test_collectors_used_names_papers_provider():
    registry, _ = registry_with(
        "paper_search", result=[{"title": "Paper", "url": "https://arxiv.org/abs/2401.00002"}]
    )
    client = _all_real_client()
    workflow = ResearchWorkflow(web_client=client, tools=registry)

    result = await workflow.run(make_task())

    assert result.status == WorkflowStatus.COMPLETED
    collectors_line = next(
        line for line in result.output.splitlines() if line.startswith("- collectors_used:")
    )
    assert "mcp:paper_search" in collectors_line

    await client.aclose()
