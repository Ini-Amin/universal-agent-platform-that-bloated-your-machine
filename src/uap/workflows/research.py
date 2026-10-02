"""Research Workflow (Master section 7) - the first domain workflow.

Linear pipeline with fan-out evidence collection:

    question_analysis -> research_planning -> fan_out -> evidence_extraction
        -> evidence_filtering -> cross_verification -> synthesis -> review

Every node is deterministic; the evidence sources are injectable async
collectors. The real web / paper / docs collectors land in Step 8+ behind the
same `Collector` signature.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from uap.contracts.models import (
    Artifact,
    ArtifactStatus,
    TaskSpec,
    VerificationCriterion,
    VerificationResult,
    WorkflowResult,
    WorkflowState,
)
from uap.workflows.runner import NodeResult, StateStore, WorkflowRunner

Collector = Callable[[str], Awaitable[list[dict[str, Any]]]]
Evidence = dict[str, Any]

WORKFLOW_NAME = "research"

NODES: tuple[str, ...] = (
    "question_analysis",
    "research_planning",
    "fan_out",
    "evidence_extraction",
    "evidence_filtering",
    "cross_verification",
    "synthesis",
    "review",
)

_ARTIFACT_SOURCE = "research_workflow"


# --------------------------------------------------------------------------- #
# Deterministic stub collectors (replaced by real sources in Step 8+)
# --------------------------------------------------------------------------- #


def _item(
    source: str,
    claim: str,
    url: str,
    confidence: float,
    provider: str | None = None,
) -> Evidence:
    item: Evidence = {
        "source": source,
        "claim": claim,
        "url": url,
        "confidence": confidence,
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }
    if provider is not None:
        item["provider"] = provider
    return item


class CollectorResults(list):
    """A list of Evidence items annotated with the provider that produced them."""

    def __init__(self, items: list[Evidence], provider: str) -> None:
        super().__init__(items)
        self.provider = provider


async def stub_web_collector(question: str) -> list[Evidence]:
    """Fixture web evidence."""
    return [
        _item("web", "Python is a programming language", "https://example.com/python", 0.92, provider="stub:web"),
        _item("web", "GIL limits true parallelism", "https://example.com/gil", 0.18, provider="stub:web"),
    ]


async def stub_papers_collector(question: str) -> list[Evidence]:
    """Fixture paper evidence."""
    return [
        _item("papers", "python is a programming language", "https://arxiv.org/abs/0001", 0.88, provider="stub:papers"),
        _item("papers", "Quantum error correction is progressing", "https://arxiv.org/abs/0002", 0.61, provider="stub:papers"),
    ]


async def stub_docs_collector(question: str) -> list[Evidence]:
    """Fixture documentation evidence."""
    return [
        _item("docs", "Python is a programming language", "https://docs.python.org/3/", 0.8, provider="stub:docs"),
        # duplicate url of a paper item - filtered out during deduplication
        _item("docs", "python is a programming language", "https://arxiv.org/abs/0001", 0.7, provider="stub:docs"),
    ]


# --------------------------------------------------------------------------- #
# Capability Resolution (MCP-first, HTTP-second, Stub-fallback)
# --------------------------------------------------------------------------- #


def find_search_tool(tools: Any | None) -> Any | None:
    """Find the first MCP tool registered for search capability.

    Matching convention:
      1. Qualified names ending with ``.search`` (e.g. ``exa.search``, ``brave.search``).
      2. Any tool whose name contains ``search``.
    """
    if tools is None:
        return None
    try:
        candidates = tools.list()
    except Exception:
        return None
    for spec in candidates:
        if getattr(spec, "name", "").endswith(".search"):
            return spec
    for spec in candidates:
        if "search" in getattr(spec, "name", "").lower():
            return spec
    return None


def find_fetch_tool(tools: Any | None) -> Any | None:
    """Find the first MCP tool registered for fetch/crawl/scrape capability.

    Matching convention:
      1. Qualified names ending with ``.fetch`` (e.g. ``firecrawl.fetch``).
      2. Any tool containing ``fetch``, ``crawl``, or ``scrape``.
    """
    if tools is None:
        return None
    try:
        candidates = tools.list()
    except Exception:
        return None
    for spec in candidates:
        if getattr(spec, "name", "").endswith(".fetch"):
            return spec
    for spec in candidates:
        name_lower = getattr(spec, "name", "").lower()
        if any(term in name_lower for term in ("fetch", "crawl", "scrape")):
            return spec
    return None


def _confidence_from_rank(rank: int) -> float:
    """Deterministic confidence score derived from search result rank.

    Formula: confidence = round(max(0.35, 0.95 - (rank * 0.10)), 2)
    Rank 0 -> 0.95, Rank 1 -> 0.85, Rank 2 -> 0.75, Rank 3 -> 0.65, etc.
    Always strictly above default threshold (0.30) for top results.
    """
    return round(max(0.35, 0.95 - (rank * 0.10)), 2)


async def call_mcp_search(
    tools: Any, tool: Any, query: str, max_results: int = 5
) -> list[dict[str, Any]]:
    """Invoke an MCP search tool through ToolRegistry.call."""
    args: dict[str, Any] = {"query": query}
    schema = getattr(tool, "input_schema", {}) or {}
    if "q" in schema and "query" not in schema:
        args = {"q": query}
    if "max_results" in schema:
        args["max_results"] = max_results
    elif "maxResults" in schema:
        args["maxResults"] = max_results
    elif "limit" in schema:
        args["limit"] = max_results

    result = await tools.call(tool.name, args)
    if not getattr(result, "ok", False):
        raise RuntimeError(f"MCP tool {tool.name} failed: {getattr(result, 'error', 'unknown error')}")

    output = getattr(result, "result", None)
    if output is None:
        output = getattr(result, "output", None)
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except Exception:
            return [{"title": query, "snippet": output, "url": "https://mcp-search.local"}]

    if isinstance(output, dict):
        if "results" in output and isinstance(output["results"], list):
            return output["results"]
        return [output]
    if isinstance(output, list):
        return [item if isinstance(item, dict) else {"title": str(item), "url": "https://mcp-search.local"} for item in output]
    return []


async def call_mcp_fetch(tools: Any, tool: Any, url: str) -> dict[str, Any]:
    """Invoke an MCP fetch tool through ToolRegistry.call."""
    args: dict[str, Any] = {"url": url}
    schema = getattr(tool, "input_schema", {}) or {}
    if "target_url" in schema:
        args = {"target_url": url}

    result = await tools.call(tool.name, args)
    if not getattr(result, "ok", False):
        raise RuntimeError(f"MCP tool {tool.name} failed: {getattr(result, 'error', 'unknown error')}")

    output = getattr(result, "result", None)
    if output is None:
        output = getattr(result, "output", None)
    if isinstance(output, str):
        try:
            return json.loads(output)
        except Exception:
            return {"url": url, "title": "", "content": {"text": output}}
    if isinstance(output, dict):
        return output
    return {"url": url, "content": {"text": str(output)}}


def _extract_text_content(fetched: dict[str, Any]) -> tuple[str, str]:
    """Extract (title, first_paragraph) from fetch response dictionary."""
    title = str(fetched.get("title") or "").strip()
    raw_content = fetched.get("content")
    text = ""
    if isinstance(raw_content, dict):
        text = str(raw_content.get("text") or raw_content.get("markdown") or "")
    elif isinstance(raw_content, str):
        text = raw_content
    elif "text" in fetched:
        text = str(fetched.get("text") or "")
    elif "markdown" in fetched:
        text = str(fetched.get("markdown") or "")

    first_paragraph = ""
    for para in text.split("\n\n"):
        p = para.strip()
        if p and not p.startswith("#"):
            first_paragraph = p
            break
    if not first_paragraph and text:
        first_paragraph = text.strip()[:200]

    return title, first_paragraph


async def web_collector(
    question: str,
    web_client: Any | None = None,
    tools: Any | None = None,
) -> list[Evidence]:
    """Collect web evidence via MCP search tool, HTTP WebClient, or fallback stub."""
    # 1. MCP search tool (portable path)
    search_tool = find_search_tool(tools)
    if search_tool is not None and tools is not None:
        try:
            results = await call_mcp_search(tools, search_tool, question)
            items: list[Evidence] = []
            for rank, res in enumerate(results):
                url = str(res.get("url") or res.get("link") or "").strip()
                if not url:
                    continue
                claim = (res.get("title") or res.get("snippet") or "Web search finding").strip()
                conf = _confidence_from_rank(rank)
                items.append(_item("web", claim, url, conf, provider=f"mcp:{search_tool.name}"))
            if items:
                return CollectorResults(items, provider=f"mcp:{search_tool.name}")
        except Exception:
            pass

    # 2. HTTP WebClient (configured via env or injected)
    client = web_client
    if client is None:
        try:
            from uap.models.web import WebClient
            client = WebClient()
        except Exception:
            client = None

    if client is not None and getattr(client, "is_search_configured", False):
        try:
            results = await client.search(question)
            provider_tag = f"http:{client.search_provider}"
            items = []
            for rank, res in enumerate(results):
                url = str(res.get("url") or "").strip()
                if not url:
                    continue
                claim = (res.get("title") or res.get("snippet") or "Web search finding").strip()
                conf = _confidence_from_rank(rank)
                items.append(_item("web", claim, url, conf, provider=provider_tag))
            if items:
                return CollectorResults(items, provider=provider_tag)
        except Exception:
            pass

    # 3. Deterministic stub fallback
    stub_items = await stub_web_collector(question)
    return CollectorResults(stub_items, provider="stub:web")


async def docs_collector(
    question: str,
    web_client: Any | None = None,
    tools: Any | None = None,
) -> list[Evidence]:
    """Collect documentation evidence via search + fetch, falling back to stub."""
    # 1. Search capability resolution (MCP first, HTTP second)
    search_tool = find_search_tool(tools)
    search_query = f"{question} documentation"
    search_results: list[dict[str, Any]] = []
    search_provider_tag = ""

    if search_tool is not None and tools is not None:
        try:
            search_results = await call_mcp_search(tools, search_tool, search_query)
            search_provider_tag = f"mcp:{search_tool.name}"
        except Exception:
            search_results = []

    client = web_client
    if not search_results:
        if client is None:
            try:
                from uap.models.web import WebClient
                client = WebClient()
            except Exception:
                client = None
        if client is not None and getattr(client, "is_search_configured", False):
            try:
                search_results = await client.search(search_query)
                search_provider_tag = f"http:{client.search_provider}"
            except Exception:
                search_results = []

    if not search_results:
        return CollectorResults(await stub_docs_collector(question), provider="stub:docs")

    valid_results = [
        r for r in search_results
        if str(r.get("url") or "").strip().startswith(("http://", "https://"))
    ]
    if not valid_results:
        return CollectorResults(await stub_docs_collector(question), provider="stub:docs")

    # Fetch top 1-2 results (expensive operation, limited to max 2)
    fetch_tool = find_fetch_tool(tools)
    items: list[Evidence] = []
    final_provider_tag = search_provider_tag

    for rank, res in enumerate(valid_results[:2]):
        url = str(res.get("url")).strip()
        fetched: dict[str, Any] | None = None
        fetch_tag = ""

        if fetch_tool is not None and tools is not None:
            try:
                fetched = await call_mcp_fetch(tools, fetch_tool, url)
                fetch_tag = f"mcp:{fetch_tool.name}"
            except Exception:
                fetched = None

        if fetched is None and client is not None and getattr(client, "is_fetch_configured", False):
            try:
                fetched = await client.fetch(url)
                fetch_tag = f"http:{client.fetch_provider}"
            except Exception:
                fetched = None

        if fetched:
            title, first_para = _extract_text_content(fetched)
            claim = title or first_para or res.get("title") or "Documentation evidence"
            conf = _confidence_from_rank(rank)
            items.append(_item("docs", claim, url, conf, provider=fetch_tag or search_provider_tag))
            if fetch_tag:
                final_provider_tag = fetch_tag
        else:
            claim = (res.get("title") or res.get("snippet") or "Documentation finding").strip()
            conf = _confidence_from_rank(rank)
            items.append(_item("docs", claim, url, conf, provider=search_provider_tag))

    if items:
        return CollectorResults(items, provider=final_provider_tag)

    return CollectorResults(await stub_docs_collector(question), provider="stub:docs")


DEFAULT_COLLECTORS: tuple[Collector, ...] = (
    stub_web_collector,
    stub_papers_collector,
    stub_docs_collector,
)

# --------------------------------------------------------------------------- #
# Shared text handling
# --------------------------------------------------------------------------- #


_WHITESPACE = re.compile(r"\s+")


def normalize_claim(text: str) -> str:
    """Case-insensitive, whitespace-stable key used for claim matching."""
    return _WHITESPACE.sub(" ", text.strip()).casefold()


def _question_from(state: WorkflowState) -> str:
    task = state.data.get("task") or {}
    return str(task.get("input", {}).get("question") or task.get("goal") or "")


# --------------------------------------------------------------------------- #
# The workflow
# --------------------------------------------------------------------------- #


class ResearchWorkflow:
    """Master section 7 research pipeline, driven by `WorkflowRunner`."""

    def __init__(
        self,
        collectors: list[Collector] | None = None,
        confidence_threshold: float = 0.3,
        state_store: StateStore | None = None,
        max_retries: int = 2,
        synthesizer: Callable[[str, list[dict[str, Any]]], Awaitable[str]] | None = None,
        web_client: Any | None = None,
        tools: Any | None = None,
    ) -> None:
        self.web_client = web_client
        self._tools = tools
        self._collectors_were_default = collectors is None
        if collectors is not None:
            self.collectors = tuple(collectors)
        elif self._has_real_provider():
            self.collectors = (
                self._make_web_collector(),
                stub_papers_collector,
                self._make_docs_collector(),
            )
        else:
            self.collectors = DEFAULT_COLLECTORS
        if not self.collectors:
            raise ValueError("at least one evidence collector is required")
        self.confidence_threshold = confidence_threshold
        #: True when any collector is a built-in stub (Master spec section 49:
        #: stub evidence must be labelled as simulation, never presented as
        #: real research). Custom/real collectors flip this to False.
        self.simulated = any(
            getattr(collector, "__name__", "").startswith("stub_")
            for collector in self.collectors
        )
        #: Optional async ``(question, verified_claims) -> analysis markdown``.
        #: When supplied, the synthesis node appends the returned analysis to
        #: the deterministic evidence report (it never replaces it, so the
        #: review node's coverage criteria keep holding). The server wires a
        #: real LLM here; the default ``None`` keeps the suite deterministic.
        self.synthesizer = synthesizer
        self.runner = WorkflowRunner(
            WORKFLOW_NAME,
            nodes={
                "question_analysis": self._question_analysis,
                "research_planning": self._research_planning,
                "fan_out": self._fan_out,
                "evidence_extraction": self._evidence_extraction,
                "evidence_filtering": self._evidence_filtering,
                "cross_verification": self._cross_verification,
                "synthesis": self._synthesis,
                "review": self._review,
            },
            entry="question_analysis",
            state_store=state_store,
            max_retries=max_retries,
        )

    def _make_web_collector(self) -> Collector:
        async def web_collector_runner(question: str) -> list[Evidence]:
            return await web_collector(question, web_client=self.web_client, tools=self.tools)

        web_collector_runner.__name__ = "web_collector"
        return web_collector_runner

    def _make_docs_collector(self) -> Collector:
        async def docs_collector_runner(question: str) -> list[Evidence]:
            return await docs_collector(question, web_client=self.web_client, tools=self.tools)

        docs_collector_runner.__name__ = "docs_collector"
        return docs_collector_runner

    def _has_real_provider(self) -> bool:
        if self._tools is not None and find_search_tool(self._tools) is not None:
            return True
        if self.web_client is not None and getattr(self.web_client, "is_search_configured", False):
            return True
        if os.environ.get("UAP_SEARCH_URL") or os.environ.get("UAP_WEB_BASE_URL"):
            return True
        return False

    @property
    def tools(self) -> Any | None:
        return self._tools

    @tools.setter
    def tools(self, value: Any | None) -> None:
        self._tools = value
        if self._collectors_were_default and self._has_real_provider():
            self.collectors = (
                self._make_web_collector(),
                stub_papers_collector,
                self._make_docs_collector(),
            )


    def graph_spec(self) -> dict[str, Any]:
        """Return the canonical graph shape for this workflow (no execution)."""
        collector_names = [
            getattr(c, "__name__", "collector") for c in self.collectors
        ]
        synth_mode = "llm" if self.synthesizer is not None else "deterministic"
        nodes = [
            {"id": "input", "kind": "input", "title": "User Input",
             "config": {}, "position": [100, 300],
             "inputs": [], "outputs": [{"name": "question", "type": "text", "required": True}]},
            {"id": "question_analysis", "kind": "agent", "title": "Question Analysis",
             "config": {"step": "question_analysis"}, "position": [260, 300],
             "inputs": [{"name": "question", "type": "text", "required": True}],
             "outputs": [{"name": "question", "type": "text", "required": True}]},
            {"id": "research_planning", "kind": "agent", "title": "Research Planning",
             "config": {"step": "research_planning", "strategy": "parallel" if len(self.collectors) >= 2 else "sequential"},
             "position": [420, 300],
             "inputs": [{"name": "question", "type": "text", "required": True}],
             "outputs": [{"name": "plan", "type": "json", "required": True}]},
            {"id": "fan_out", "kind": "parallel", "title": "Fan Out (Evidence Collection)",
             "config": {"collectors": collector_names}, "position": [580, 300],
             "inputs": [{"name": "plan", "type": "json", "required": True}],
             "outputs": [{"name": "evidence", "type": "json", "required": True}]},
            {"id": "evidence_extraction", "kind": "tool", "title": "Evidence Extraction",
             "config": {"step": "evidence_extraction"}, "position": [740, 300],
             "inputs": [{"name": "evidence", "type": "json", "required": True}],
             "outputs": [{"name": "evidence", "type": "json", "required": True}]},
            {"id": "evidence_filtering", "kind": "tool", "title": "Evidence Filtering",
             "config": {"step": "evidence_filtering", "confidence_threshold": self.confidence_threshold},
             "position": [900, 300],
             "inputs": [{"name": "evidence", "type": "json", "required": True}],
             "outputs": [{"name": "filtered_evidence", "type": "json", "required": True}]},
            {"id": "cross_verification", "kind": "evaluation", "title": "Cross Verification",
             "config": {"step": "cross_verification", "min_sources": 2}, "position": [1060, 300],
             "inputs": [{"name": "filtered_evidence", "type": "json", "required": True}],
             "outputs": [{"name": "verified_claims", "type": "json", "required": True}]},
            {"id": "synthesis", "kind": "synthesis", "title": "Synthesis",
             "config": {"synthesizer": synth_mode}, "position": [1220, 300],
             "inputs": [{"name": "verified_claims", "type": "json", "required": True}],
             "outputs": [{"name": "report", "type": "text", "required": True}]},
            {"id": "review", "kind": "evaluation", "title": "Review",
             "config": {"step": "review", "verifier": "research_review"}, "position": [1380, 300],
             "inputs": [{"name": "report", "type": "text", "required": True}],
             "outputs": [{"name": "verification", "type": "json", "required": True}]},
            {"id": "output", "kind": "output", "title": "Result",
             "config": {}, "position": [1540, 300],
             "inputs": [{"name": "verification", "type": "json", "required": True}],
             "outputs": []},
        ]
        # Linear chain: input -> qa -> rp -> fan_out -> ee -> ef -> cv -> synth -> review -> output
        node_ids = [n["id"] for n in nodes]
        edges = []
        for i in range(len(node_ids) - 1):
            src = nodes[i]
            tgt = nodes[i + 1]
            src_port = src["outputs"][0]["name"] if src["outputs"] else "out"
            tgt_port = tgt["inputs"][0]["name"] if tgt["inputs"] else "in"
            edges.append({
                "id": f"e{i+1}", "source": node_ids[i], "source_port": src_port,
                "target": node_ids[i + 1], "target_port": tgt_port, "kind": "data",
            })
        return {
            "id": f"wf-{WORKFLOW_NAME}",
            "name": WORKFLOW_NAME,
            "description": "Research pipeline: question analysis through cross-verified synthesis",
            "nodes": nodes,
            "edges": edges,
        }

    # ------------------------------------------------------------------ #
    # Entry points
    # ------------------------------------------------------------------ #

    async def run(self, task: TaskSpec) -> WorkflowResult:
        return await self.runner.run(task)

    async def resume(self, task_id: str) -> WorkflowResult:
        return await self.runner.resume(task_id)

    # ------------------------------------------------------------------ #
    # Nodes
    # ------------------------------------------------------------------ #

    async def _question_analysis(self, state: WorkflowState) -> NodeResult:
        question = _question_from(state)
        return NodeResult({"question": question}, next_node="research_planning")

    async def _research_planning(self, state: WorkflowState) -> NodeResult:
        question = state.data.get("question", "")
        terms = [term for term in _WHITESPACE.split(question.strip()) if term]
        plan = {
            "question": question,
            "sub_queries": [question] if question else [],
            "collectors": [getattr(c, "__name__", "collector") for c in self.collectors],
            "strategy": "parallel" if len(self.collectors) >= 2 else "sequential",
        }
        if len(terms) > 2:
            plan["sub_queries"].append(" ".join(terms[:3]))
        return NodeResult({"plan": plan}, next_node="fan_out")

    async def _fan_out(self, state: WorkflowState) -> NodeResult:
        """Collect evidence from every independent source concurrently."""
        question = state.data.get("question", "")
        if len(self.collectors) >= 2:
            batches = await asyncio.gather(*(c(question) for c in self.collectors))
        else:
            batches = [await self.collectors[0](question)]

        collectors_used: list[str] = []
        evidence: list[Evidence] = []
        for i, batch in enumerate(batches):
            collector = self.collectors[i]
            collector_name = getattr(collector, "__name__", f"collector_{i}")
            provider = getattr(batch, "provider", None)
            if not provider:
                for item in batch:
                    if isinstance(item, dict) and "provider" in item:
                        provider = item["provider"]
                        break
            if not provider:
                if collector_name.startswith("stub_"):
                    provider = f"stub:{collector_name.removeprefix('stub_').removesuffix('_collector')}"
                else:
                    provider = collector_name
            collectors_used.append(provider)
            evidence.extend(batch)

        # §49 honest labelling: simulated iff any collector used a stub
        run_simulated = any(
            c.startswith("stub_") or c.startswith("stub:")
            for c in collectors_used
        )
        self.simulated = run_simulated
        state.data["collectors_used"] = collectors_used
        state.data["simulated"] = run_simulated

        return NodeResult({"evidence": evidence}, next_node="evidence_extraction")


    async def _evidence_extraction(self, state: WorkflowState) -> NodeResult:
        """Normalise raw items without changing their meaning or provenance."""
        cleaned: list[Evidence] = []
        for item in state.data.get("evidence", []):
            cleaned.append(
                {
                    **item,
                    "claim": _WHITESPACE.sub(" ", str(item.get("claim", "")).strip()),
                    "source": _WHITESPACE.sub(" ", str(item.get("source", "")).strip()),
                    "url": str(item.get("url", "")).strip(),
                    "confidence": float(item.get("confidence", 0.0)),
                }
            )
        return NodeResult({"evidence": cleaned}, next_node="evidence_filtering")

    async def _evidence_filtering(self, state: WorkflowState) -> NodeResult:
        """Drop low-confidence items and deduplicate by url, keeping provenance."""
        kept: list[Evidence] = []
        seen_urls: set[str] = set()
        for item in state.data.get("evidence", []):
            if item.get("confidence", 0.0) < self.confidence_threshold:
                continue
            url = item.get("url", "")
            if url in seen_urls:
                continue
            seen_urls.add(url)
            kept.append(item)
        return NodeResult({"filtered_evidence": kept}, next_node="cross_verification")

    async def _cross_verification(self, state: WorkflowState) -> NodeResult:
        """A claim is verified when >= 2 distinct sources agree on it."""
        by_claim: dict[str, list[Evidence]] = defaultdict(list)
        for item in state.data.get("filtered_evidence", []):
            by_claim[normalize_claim(str(item.get("claim", "")))].append(item)

        verified: list[dict[str, Any]] = []
        unverified: list[dict[str, Any]] = []
        for group in by_claim.values():
            sources = sorted({str(item.get("source", "")) for item in group})
            record = {
                "claim": group[0].get("claim", ""),
                "sources": sources,
                "agreement": len(sources),
                "evidence": group,
            }
            (verified if len(sources) >= 2 else unverified).append(record)
        verified.sort(key=lambda record: record["claim"].casefold())
        unverified.sort(key=lambda record: record["claim"].casefold())

        return NodeResult(
            {
                "verified_claims": verified,
                "unverified_claims": unverified,
            },
            next_node="synthesis",
        )

    async def _synthesis(self, state: WorkflowState) -> NodeResult:
        question = state.data.get("question", "")
        verified = state.data.get("verified_claims", [])
        unverified = state.data.get("unverified_claims", [])
        evidence = state.data.get("filtered_evidence", [])
        simulated = state.data.get("simulated", self.simulated)
        collectors_used = state.data.get("collectors_used", [])
        report = _build_report(
            question,
            verified,
            unverified,
            evidence,
            simulated=simulated,
            collectors_used=collectors_used,
        )


        if self.synthesizer is not None:
            try:
                analysis = await self.synthesizer(question, verified)
            except Exception as exc:  # noqa: BLE001 - analysis is additive
                analysis = f"*LLM analysis unavailable: {type(exc).__name__}*"
            report = f"{report}\n## Analysis\n\n{analysis}\n"

        task_id = state.task_id
        artifacts = [
            Artifact(
                task_id=task_id,
                type="report.md",
                source=_ARTIFACT_SOURCE,
                status=ArtifactStatus.FINAL,
                content_ref=report,
            ),
            Artifact(
                task_id=task_id,
                type="sources.json",
                source=_ARTIFACT_SOURCE,
                status=ArtifactStatus.FINAL,
                content_ref=json.dumps(evidence, indent=2, sort_keys=True),
            ),
        ]
        return NodeResult(
            {"synthesis": report, "output": report, "artifacts": artifacts},
            next_node="review",
        )

    async def _review(self, state: WorkflowState) -> NodeResult:
        evidence = state.data.get("filtered_evidence", [])
        verified = state.data.get("verified_claims", [])
        report = state.data.get("output", "")

        with_url = sum(1 for item in evidence if item.get("url"))
        covered = sum(
            1
            for record in verified
            if normalize_claim(str(record.get("claim", ""))) in normalize_claim(report)
        )
        criteria = [
            VerificationCriterion(
                criterion="evidence_count",
                passed=len(evidence) >= 1,
                evidence=f"filtered evidence count = {len(evidence)}",
            ),
            VerificationCriterion(
                criterion="sources_have_urls",
                passed=bool(evidence) and with_url == len(evidence),
                evidence=f"{with_url}/{len(evidence)} evidence items carry a url",
            ),
            VerificationCriterion(
                criterion="synthesis_covers_all_verified",
                passed=not verified or covered == len(verified),
                evidence=f"{covered}/{len(verified)} verified claims appear in the report",
            ),
        ]
        failed = [c.criterion for c in criteria if not c.passed]
        verification = VerificationResult(
            verifier="research_review",
            passed=not failed,
            criteria=criteria,
            score=sum(1 for c in criteria if c.passed) / len(criteria) if criteria else 1.0,
            notes=(
                "all review criteria passed"
                if not failed
                else "failed criteria: " + ", ".join(failed)
            ),
        )
        return NodeResult({"verification": verification}, next_node=None)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def _bullet_lines(claim: str, sources: list[str], evidence: list[Evidence]) -> list[str]:
    lines = [f"- {claim}", f"  - sources: {', '.join(sources)}"]
    for item in evidence:
        url = item.get("url")
        if url:
            lines.append(f"  - {url}")
    return lines


def _build_report(
    question: str,
    verified: list[dict[str, Any]],
    unverified: list[dict[str, Any]],
    evidence: list[Evidence],
    *,
    simulated: bool = False,
    collectors_used: list[str] | None = None,
) -> str:
    sources = sorted({str(item.get("source", "")) for item in evidence})
    lines = ["# Research Report", ""]
    if simulated:
        # Master spec section 49: deterministic stub evidence must never be
        # presented as real research. Say it at the top, in the artifact itself.
        stub_lines = [
            "> **SIMULATION — DETERMINISTIC STUB EVIDENCE**",
            "> This run used built-in deterministic stub collectors. The findings and",
            "> URLs below are FIXED SAMPLE DATA, not real web research. Configure real",
            "> collectors before treating any claim as verified.",
        ]
        if collectors_used:
            real_collectors = [
                c for c in collectors_used
                if not (c.startswith("stub_") or c.startswith("stub:"))
            ]
            if real_collectors:
                stub_lines.append(f"> (Real sources active: {', '.join(real_collectors)})")
        stub_lines.append("")
        lines.extend(stub_lines)

    lines.extend(
        [
            "## Question",
            "",
            question,
            "",
            "## Verified findings",
            "",
        ]
    )
    if verified:
        for record in verified:
            lines.extend(_bullet_lines(str(record["claim"]), record["sources"], record["evidence"]))
    else:
        lines.append("- none")
    lines.extend(["", "## Unverified findings", ""])
    if unverified:
        for record in unverified:
            lines.extend(_bullet_lines(str(record["claim"]), record["sources"], record["evidence"]))
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Evidence base",
            "",
            f"{len(evidence)} evidence items across {len(sources)} sources"
            f" ({', '.join(sources) or 'none'}).",
        ]
    )
    if collectors_used:
        lines.append(f"- collectors_used: {', '.join(collectors_used)}")
    lines.append("")
    return "\n".join(lines)
