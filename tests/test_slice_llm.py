"""Slice-level integration tests for LLM agent wiring.

DB-backed tests skip cleanly when PostgreSQL is unreachable.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
import pytest

from uap.contracts.models import TokenUsage
from uap.models.client import ChatClient, LLMClientError
from uap.slice import PlatformSlice, SliceResult, build_research_graph


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_LLM_TEXT = "The LLM produced this real answer about caching strategies."

_CANNED = {
    "choices": [{"message": {"content": _LLM_TEXT}}],
    "usage": {"prompt_tokens": 20, "completion_tokens": 15},
}


def _mock_transport(*, json_body: dict | None = None, raise_exc: Exception | None = None):
    body = json_body if json_body is not None else _CANNED

    def handler(request: httpx.Request) -> httpx.Response:
        if raise_exc is not None:
            raise raise_exc
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


def _mock_client(transport: httpx.MockTransport | None = None) -> ChatClient:
    t = transport or _mock_transport()
    c = ChatClient(base_url="http://test", api_key="test-key")
    c._client = httpx.AsyncClient(transport=t, base_url="http://test")
    return c


# --------------------------------------------------------------------------- #
# DB setup (same pattern as test_vertical_slice.py)
# --------------------------------------------------------------------------- #

DEFAULT_TEST_URL = "postgresql+psycopg://uap:uap_local_dev@127.0.0.1:5432/uap_test"


def _resolved_test_url() -> str:
    return os.environ.get("DATABASE_URL") or DEFAULT_TEST_URL


def _db_skip_reason() -> str | None:
    try:
        from sqlalchemy import text
        from uap.db import create_db_engine

        engine = create_db_engine(
            _resolved_test_url(), connect_args={"connect_timeout": 2}
        )
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
    except Exception as exc:  # noqa: BLE001
        return f"PostgreSQL not reachable at {_resolved_test_url()}: {exc}"
    return None


_SKIP = _db_skip_reason()
db_test = pytest.mark.skipif(_SKIP is not None, reason=_SKIP or "")


@pytest.fixture()
def db_session_factory():
    from sqlalchemy import text
    from uap.db import create_db_engine, create_session_factory

    engine = create_db_engine(_resolved_test_url())
    tables = (
        "knowledge_events",
        "knowledge_provenance",
        "knowledge_items",
        "decision_traces",
        "execution_checkpoints",
        "execution_events",
        "executions",
        "workflow_versions",
        "workflow_definitions",
    )
    statement = text(
        "TRUNCATE "
        + ", ".join(f'"{name}"' for name in tables)
        + " RESTART IDENTITY CASCADE"
    )
    with engine.begin() as conn:
        conn.execute(statement)
    try:
        yield create_session_factory(engine)
    finally:
        with engine.begin() as conn:
            conn.execute(statement)
        engine.dispose()


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


@db_test
def test_llm_output_flows_through_pipeline(db_session_factory, tmp_path):
    """Mocked LLM text reaches the artifacts (the whole point)."""
    client = _mock_client()
    slice_ = PlatformSlice(
        session_factory=db_session_factory,
        artifacts_root=tmp_path,
        llm_client=client,
    )
    result = slice_.run("research and compare two approaches to caching")
    assert result.error is None, result.error
    assert result.execution_status == "completed"
    assert result.artifacts, "expected at least one artifact"
    # Verify the actual LLM text reached disk. The real research pipeline
    # appends the model's analysis to the synthesis report, so the text lands
    # in the slice-synthesis artifact.
    artifact_files = list(Path(tmp_path).rglob("*__v1__*"))
    assert artifact_files, "expected artifact files on disk"
    found = False
    for f in artifact_files:
        if _LLM_TEXT in f.read_text():
            found = True
            break
    assert found, f"LLM text {_LLM_TEXT!r} not found in any artifact file"


@db_test
def test_llm_failure_degrades_gracefully(db_session_factory, tmp_path):
    """When the LLM client raises, the pipeline still completes with a report.

    The synthesis node treats the analysis as additive: a failing synthesizer
    degrades to an explicit note, so the evidence report (and the run) survive.
    """
    transport = _mock_transport(raise_exc=httpx.TimeoutException("LLM down"))
    client = _mock_client(transport)
    slice_ = PlatformSlice(
        session_factory=db_session_factory,
        artifacts_root=tmp_path,
        llm_client=client,
    )
    result = slice_.run("research something when LLM is broken")
    # Pipeline must NOT crash — it completes (possibly with partial results).
    assert result.execution_status == "completed", (
        f"expected completed, got {result.execution_status!r}; error={result.error}"
    )
    # The degradation message should appear somewhere in the artifacts.
    artifact_files = list(Path(tmp_path).rglob("*__v1__*"))
    found_degradation = any(
        "LLM analysis unavailable" in f.read_text() for f in artifact_files
    )
    assert found_degradation, "expected degradation marker in artifacts"


@db_test
def test_default_slice_still_works_as_echo(db_session_factory, tmp_path):
    """No llm_client → EchoAgent(name='llm') — regression guard."""
    slice_ = PlatformSlice(
        session_factory=db_session_factory,
        artifacts_root=tmp_path,
    )
    result = slice_.run("research and compare two approaches to caching")
    assert result.error is None, result.error
    assert result.execution_status == "completed"
    assert result.artifacts, "expected at least one artifact"


def test_graph_uses_llm_agent_name():
    """The canonical graph declares the REAL research pipeline nodes.

    (The old demo graph's ``recon`` node targeting the ``llm`` agent is gone;
    the LLM now lives in the pipeline's synthesis node via the synthesizer.)
    """
    graph = build_research_graph()
    pipeline_ids = [n.id for n in graph.nodes if n.config.get("pipeline_node")]
    assert "synthesis" in pipeline_ids
    assert "question_analysis" in pipeline_ids


def test_degradation_is_agent_level():
    """Degradation is handled inside LLMAgent (PARTIAL status), not ERROR edges.

    The graph has no ERROR edges — all edges are DATA.
    """
    graph = build_research_graph()
    from uap.graph import EdgeKind
    error_edges = [e for e in graph.edges if e.kind == EdgeKind.ERROR]
    assert error_edges == [], "agent-level degradation: no ERROR edges expected"


@pytest.mark.skipif(
    not os.environ.get("UAP_LIVE"),
    reason="live LLM test: set UAP_LIVE=1 to enable",
)
def test_live_llm_smoke():
    """Actually call 9router with cbai/deepseek-v4.1-flash (skipped by default)."""
    client = ChatClient()  # picks up env for base_url + api_key
    text, usage = asyncio.run(
        client.complete(
            "cbai/deepseek-v4.1-flash",
            [{"role": "user", "content": "Say hello in exactly 5 words."}],
            max_tokens=64,
        )
    )
    assert text.strip(), "expected non-empty LLM output"
    assert usage.tokens_out > 0
