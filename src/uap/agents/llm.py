"""LLM-backed agent for real AI reasoning (Master section 10).

Graceful degradation: if the LLM call fails, ``_timed_run`` returns
``AgentStatus.PARTIAL`` with echo-style fallback text instead of raising.
This keeps the pipeline flowing — ``PlatformNodeRuntime._run_agent`` only
raises ``NodeExecutionError`` on ``AgentStatus.FAILED``, so PARTIAL passes
through. The agent-level fallback was chosen over an ERROR edge because the
execution engine's ``_normal_ready`` rejects a node when *any* required
incoming edge is dead, and the summarize node's DATA edge from ``fetch``
goes dead when ``recon`` fails — making the summarize node unreachable
even with an ERROR edge unless the port is made non-required, which
complicates the graph for little gain.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from uap.models.router import ModelRouter, RoutingDecision
    from uap.trace.model import DecisionTrace

from uap.agents.base import BaseAgent
from uap.contracts import AgentContext, AgentResult, AgentStatus
from uap.contracts.models import TokenUsage
from uap.models.client import ChatClient, LLMClientError

__all__ = ["LLMAgent"]

#: Model used when neither an explicit model_id nor a router is supplied.
#: Override with UAP_DEFAULT_MODEL to match your gateway's catalog.
_DEFAULT_MODEL = os.environ.get("UAP_DEFAULT_MODEL", "gpt-5.6-sol")
_SYSTEM_PROMPT = (
    "You are a research agent inside an orchestration platform. "
    "Answer concisely."
)


class LLMAgent(BaseAgent):
    """Agent that delegates reasoning to an LLM via :class:`ChatClient`."""

    name = "llm"
    capabilities = ["reasoning", "general"]

    def __init__(
        self,
        client: ChatClient | None = None,
        router: ModelRouter | None = None,
        *,
        model_id: str | None = None,
        capability: Any | None = None,
        decision_sink: Any | None = None,
        name: str | None = None,
    ) -> None:
        super().__init__(name=name)
        self.client = client or ChatClient()
        self.router = router
        self.model_id = model_id
        self.capability = capability
        self.decision_sink = decision_sink
        self.last_decision: RoutingDecision | None = None
        self.last_trace: DecisionTrace | None = None
        self.fallback_reason: str | None = None
        self.last_fallback: dict[str, Any] | None = None

    async def _timed_run(self, context: AgentContext) -> AgentResult:
        messages = self._build_messages(context)
        cap = context.extras.get("capability") if context.extras else None
        exec_id = context.extras.get("execution_id") if context.extras else None
        model = self._resolve_model(capability=cap, execution_id=exec_id)
        try:
            text, usage = await self.client.complete(model, messages)
        except (LLMClientError, Exception) as exc:
            # ponytail: agent-level degradation; upgrade to ERROR edge when
            # graph supports non-required fan-in ports on the summarize node.
            fallback = context.task.goal or "no goal"
            return AgentResult(
                agent_name=self.name,
                status=AgentStatus.PARTIAL,
                output=f"[LLM unavailable: {type(exc).__name__}] {fallback}",
                error=str(exc),
            )
        return AgentResult(
            agent_name=self.name,
            status=AgentStatus.SUCCESS,
            output=text,
            usage=usage,
        )

    # -- internal helpers -------------------------------------------------- #

    @staticmethod
    def _build_messages(context: AgentContext) -> list[dict]:
        parts = [context.task.goal or ""]
        for section in context.context_sections:
            parts.append(f"## {section.key}\n{section.content}")
        user_content = "\n\n".join(p for p in parts if p)
        return [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

    def _resolve_model(
        self,
        capability: Any | None = None,
        execution_id: str | None = None,
    ) -> str:
        if self.model_id:
            return self.model_id
        if self.router:
            from uap.models.catalog import ModelCapability
            from uap.models.router import RoutingRequest
            from uap.trace.model import DecisionAlternative, DecisionTrace, DecisionType

            cap = capability or self.capability or ModelCapability.REASONING
            if isinstance(cap, str):
                try:
                    cap = ModelCapability(cap)
                except ValueError:
                    pass

            decision, is_fallback = self.router.select_or_fallback(
                RoutingRequest(capability=cap)  # type: ignore[arg-type]
            )
            self.last_decision = decision
            if is_fallback:
                self.fallback_reason = decision.reason
                self.last_fallback = {
                    "capability": str(cap),
                    "fallback_model": decision.model_id,
                    "reason": decision.reason,
                }
            else:
                self.fallback_reason = None
                self.last_fallback = None

            trace = DecisionTrace(
                execution_id=execution_id or "local",
                node_id=self.name,
                decision_type=DecisionType.MODEL_SELECTION,
                chosen=decision.model_id,
                rationale=decision.reason,
                alternatives=[
                    DecisionAlternative(
                        option=fb,
                        reason_rejected="ranked lower by preference or policy",
                    )
                    for fb in decision.fallbacks
                ],
                confidence=1.0 if not is_fallback else 0.0,
                inputs_summary={"capability": str(cap), "fallback": is_fallback},
            )
            self.last_trace = trace
            if self.decision_sink is not None:
                try:
                    self.decision_sink(trace)
                except Exception:
                    pass

            return decision.model_id
        return os.environ.get("UAP_DEFAULT_MODEL", _DEFAULT_MODEL)
