"""Tests for build Step 5 - Agent abstraction (Master section 10)."""

import re
from pathlib import Path
from typing import Any

import pytest

import uap.agents
from uap.agents import Agent, AgentRegistry, BaseAgent, EchoAgent, SummarizeAgent
from uap.contracts import (
    AgentContext,
    AgentResult,
    AgentStatus,
    ContextSection,
    Domain,
    TaskSpec,
)

FORBIDDEN_IMPORT = re.compile(
    r"^\s*(?:from|import)\s+(fastapi|langgraph|sqlite3|mcp|httpx|requests)\b",
    flags=re.MULTILINE,
)


def make_context(
    goal: str = "explain subnetting",
    message: str | None = None,
    sections: list[str] | None = None,
) -> AgentContext:
    extras: dict[str, Any] = {}
    if message is not None:
        extras["message"] = message
    return AgentContext(
        task=TaskSpec(domain=Domain.UNKNOWN, goal=goal),
        context_sections=[
            ContextSection(key=f"s{i}", content=content)
            for i, content in enumerate(sections or [])
        ],
        available_tools=[],
        available_skills=[],
        budget={},
        extras=extras,
    )


class BoomAgent(BaseAgent):
    """Deliberately failing agent for error-containment tests."""

    name = "boom"
    capabilities = ["explode"]

    async def _timed_run(self, context: AgentContext) -> AgentResult:
        raise RuntimeError("kaboom")


class UnnamedAgent(BaseAgent):
    """Abstract-method enforcement probe; intentionally declares no name."""


# --------------------------------------------------------------------------- #
# Concrete agents
# --------------------------------------------------------------------------- #


async def test_echo_returns_result_with_matching_name_and_message() -> None:
    result = await EchoAgent().run(make_context(goal="the goal", message="hello world"))
    assert result.agent_name == "echo"
    assert result.status == AgentStatus.SUCCESS
    assert "hello world" in result.output
    assert "the goal" in result.output


async def test_echo_without_message_is_just_the_goal() -> None:
    result = await EchoAgent().run(make_context(goal="only goal"))
    assert result.output == "only goal"


async def test_echo_fills_latency() -> None:
    result = await EchoAgent().run(make_context(message="hi"))
    assert result.usage.latency_ms >= 0.0


async def test_summarize_extracts_first_sentence_of_each_section() -> None:
    result = await SummarizeAgent().run(
        make_context(
            sections=[
                "Subnetting divides a network. This sentence is dropped.",
                "CIDR is compact notation. Also dropped.",
                "VLANs isolate traffic. Dropped too.",
            ]
        )
    )
    assert result.status == AgentStatus.SUCCESS
    assert result.agent_name == "summarize"
    assert result.output.splitlines() == [
        "Subnetting divides a network.",
        "CIDR is compact notation.",
        "VLANs isolate traffic.",
    ]


async def test_summarize_caps_at_three_sections() -> None:
    result = await SummarizeAgent().run(
        make_context(sections=[f"Section {i}. Trailing text." for i in range(5)])
    )
    assert len(result.output.splitlines()) == 3


@pytest.mark.parametrize("sections", [[], ["   ", "\n"]])
async def test_summarize_without_context_is_partial(sections: list[str]) -> None:
    result = await SummarizeAgent().run(make_context(sections=sections))
    assert result.status == AgentStatus.PARTIAL
    assert result.output == "no context"


async def test_failing_agent_reports_failure_without_raising() -> None:
    result = await BoomAgent().run(make_context())
    assert result.agent_name == "boom"
    assert result.status == AgentStatus.FAILED
    assert result.error is not None
    assert "RuntimeError" in result.error
    assert "kaboom" in result.error


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


def test_registry_register_get_and_names() -> None:
    registry = AgentRegistry()
    echo, summarize = EchoAgent(), SummarizeAgent()
    registry.register(echo)
    registry.register(summarize)

    assert registry.get("echo") is echo
    assert registry.get("summarize") is summarize
    assert registry.get("missing") is None
    assert registry.names() == ["echo", "summarize"]


def test_registry_rejects_duplicate_names() -> None:
    registry = AgentRegistry()
    registry.register(EchoAgent())
    with pytest.raises(ValueError):
        registry.register(EchoAgent())


def test_registry_select_filters_by_capability_in_registration_order() -> None:
    registry = AgentRegistry()
    registry.register(EchoAgent())
    registry.register(SummarizeAgent())
    registry.register(EchoAgent(name="echo-2", capabilities=["summarize"]))

    assert [type(agent).__name__ for agent in registry.select("summarize")] == [
        "SummarizeAgent",
        "EchoAgent",
    ]
    assert [agent.name for agent in registry.select("echo")] == ["echo"]
    assert registry.select("missing") == []


# --------------------------------------------------------------------------- #
# Naming, capabilities, protocol conformance
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", ["", "   "])
def test_base_agent_rejects_empty_names(bad: str) -> None:
    with pytest.raises(ValueError):
        EchoAgent(name=bad)


def test_base_agent_is_abstract() -> None:
    with pytest.raises(TypeError):
        UnnamedAgent()  # type: ignore[abstract]


def test_name_and_capability_overrides_do_not_leak_to_the_class() -> None:
    overridden = EchoAgent(name="echo-2", capabilities=["echo", "custom"])
    assert overridden.name == "echo-2"
    assert overridden.capabilities == ["echo", "custom"]

    # The class-level defaults are untouched for other instances.
    assert EchoAgent.capabilities == ["echo"]
    assert EchoAgent().name == "echo"
    assert EchoAgent().capabilities == ["echo"]


def test_concrete_agents_satisfy_the_agent_protocol() -> None:
    assert isinstance(EchoAgent(), Agent)
    assert isinstance(SummarizeAgent(), Agent)
    assert not isinstance(object(), Agent)


# --------------------------------------------------------------------------- #
# Serialization and hygiene
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "agent", [EchoAgent(), SummarizeAgent()], ids=lambda a: type(a).__name__
)
async def test_agent_result_round_trips_json(agent: BaseAgent) -> None:
    result = await agent.run(
        make_context(message="hi", sections=["First sentence. Second."])
    )
    restored = AgentResult.model_validate_json(result.model_dump_json())
    assert restored == result


async def test_repeated_runs_fill_usage_independently() -> None:
    agent = EchoAgent()
    first = await agent.run(make_context(message="one"))
    second = await agent.run(make_context(message="two"))

    assert first.usage.latency_ms >= 0.0
    assert second.usage.latency_ms >= 0.0
    assert first is not second
    assert first.usage is not second.usage
    assert first.output != second.output


def test_agents_import_no_ui_db_http_or_mcp_layers() -> None:
    """Master section 10: agents stay free of UI/db/HTTP/MCP/LangGraph coupling."""
    agents_dir = Path(uap.agents.__file__).parent
    offenders = [
        f"{path.name}: {match.group(1)}"
        for path in sorted(agents_dir.glob("*.py"))
        for match in FORBIDDEN_IMPORT.finditer(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
