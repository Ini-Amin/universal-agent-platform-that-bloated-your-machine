"""Deterministic agents for wiring tests and LLM-free paths.

Master section 3: use deterministic logic wherever deterministic logic
suffices; reserve LLMs for reasoning, interpretation and adaptation.
"""

import re

from uap.agents.base import BaseAgent
from uap.contracts import AgentContext, AgentResult, AgentStatus

_SENTENCE = re.compile(r"[^.!?\n]*[.!?]?")


def _first_sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    match = _SENTENCE.search(text)
    return match.group(0).strip() if match else text


class EchoAgent(BaseAgent):
    """Echoes the extras message alongside the task goal."""

    name = "echo"
    capabilities = ["echo"]

    async def _timed_run(self, context: AgentContext) -> AgentResult:
        message = context.extras.get("message", "")
        output = " | ".join(part for part in (context.task.goal, message) if part)
        return AgentResult(
            agent_name=self.name,
            status=AgentStatus.SUCCESS,
            output=output,
        )


class SummarizeAgent(BaseAgent):
    """Extractive summary: first sentence of up to three context sections."""

    name = "summarize"
    capabilities = ["summarize"]

    async def _timed_run(self, context: AgentContext) -> AgentResult:
        sentences = []
        for section in context.context_sections:
            first = _first_sentence(section.content)
            if first:
                sentences.append(first)
            if len(sentences) == 3:
                break
        if not sentences:
            return AgentResult(
                agent_name=self.name,
                status=AgentStatus.PARTIAL,
                output="no context",
            )
        return AgentResult(
            agent_name=self.name,
            status=AgentStatus.SUCCESS,
            output="\n".join(sentences),
        )
