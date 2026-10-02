"""Agent abstraction (Master section 10) - build Step 5."""

from uap.agents.base import Agent, BaseAgent
from uap.agents.deterministic import EchoAgent, SummarizeAgent
from uap.agents.llm import LLMAgent
from uap.agents.registry import AgentRegistry

__all__ = [
    "Agent",
    "AgentRegistry",
    "BaseAgent",
    "EchoAgent",
    "LLMAgent",
    "SummarizeAgent",
]
