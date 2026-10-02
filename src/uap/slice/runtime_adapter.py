"""Node runtime for the §71 vertical slice.

Routes each graph node kind to a real platform collaborator:

* ``AGENT``      -> :class:`~uap.agents.registry.AgentRegistry` lookup + ``run``.
* ``TOOL``       -> :class:`~uap.tools.registry.ToolRegistry` ``call`` (policy-gated).
* ``SYNTHESIS``  -> deterministic concatenation of upstream text/content.
* ``EVALUATION`` -> deterministic non-empty check over the inbound content.
* ``KNOWLEDGE``  -> deterministic claim extraction (statement + content) for the
  orchestrator to persist with provenance later.
* ``INPUT``/``OUTPUT`` -> pass-through (the engine seeds/collects these).

Every node returns ``{output_port: value}`` keyed by its *declared* output
ports, so edge wiring stays honest. A missing agent or a denied tool raises
:class:`~uap.execution.context.NodeExecutionError`, which the engine turns into
a failed node (never a crashed run).
"""
from __future__ import annotations

import logging
from typing import Any

from uap.observability.errors import log_swallowed_exception
from uap.agents.registry import AgentRegistry
from uap.context import ContextBudget, ContextCompiler
from uap.context.scoring import filter_secrets
from uap.contracts import (
    AgentContext,
    AgentStatus,
    ContextSection,
    TaskSpec,
    WorkflowState,
)
from uap.execution.context import ExecutionContext, NodeExecutionError
from uap.graph import GraphNode, NodeKind
from uap.tools.registry import ToolRegistry
__all__ = ["PlatformNodeRuntime"]

logger = logging.getLogger(__name__)


def _output_port_names(node: GraphNode) -> list[str]:
    """Declared output port names, defaulting to a single ``"value"`` port."""
    names = [port.name for port in node.outputs]
    return names or ["value"]


def _primary_output(node: GraphNode) -> str:
    return _output_port_names(node)[0]


def _collect_text(inputs: dict[str, Any]) -> str:
    """Flatten inbound port values into one text blob (deterministic order)."""
    parts: list[str] = []
    for key in sorted(inputs):
        value = inputs[key]
        if isinstance(value, dict) and "content" in value:
            value = value["content"]
        if value is None:
            continue
        parts.append(value if isinstance(value, str) else str(value))
    return "\n".join(part for part in parts if part)


class PlatformNodeRuntime:
    """Dispatches graph nodes to the real agent/tool/deterministic backends."""

    def __init__(
        self,
        agents: AgentRegistry,
        tools: ToolRegistry,
        *,
        task: TaskSpec | None = None,
        pipeline: Any | None = None,
        session_factory: Any | None = None,
        knowledge_store: Any | None = None,
        compiler: ContextCompiler | None = None,
        budget: ContextBudget | None = None,
    ) -> None:
        self.agents = agents
        self.tools = tools
        self.task = task
        #: Optional ResearchWorkflow (or compatible) whose real node functions
        #: execute nodes carrying ``config["pipeline_node"]``. This is what
        #: makes the durable slice run the ACTUAL research pipeline instead of
        #: generic agent/tool dispatch.
        self.pipeline = pipeline
        self.session_factory = session_factory
        self.knowledge_store = knowledge_store
        self.budget = budget or ContextBudget()
        self.compiler = compiler or ContextCompiler(self.budget)
        #: WorkflowState.data carried across pipeline nodes (the pipeline's
        #: node functions read/write shared state, exactly as the in-memory
        #: runner does).
        self._pipeline_data: dict[str, Any] = {}
        self._compiled_contexts: dict[str, AgentContext] = {}
        self._latest_context: AgentContext | None = None
        self._wrap_synthesizer_if_present()
    # ------------------------------------------------------------------ #
    # NodeRuntime protocol
    # ------------------------------------------------------------------ #

    async def run_node(
        self, node: GraphNode, inputs: dict[str, Any], ctx: ExecutionContext
    ) -> dict[str, Any]:
        # Pipeline nodes (config["pipeline_node"]) run the REAL workflow node
        # functions — this is what keeps the durable slice honest.
        pipeline_node = node.config.get("pipeline_node")
        if pipeline_node and self.pipeline is not None:
            return await self._run_pipeline_node(str(pipeline_node), node, inputs)
        kind = node.kind
        if kind is NodeKind.AGENT:
            return await self._run_agent(node, inputs, ctx)
        if kind is NodeKind.TOOL:
            return await self._run_tool(node, inputs)
        if kind is NodeKind.SYNTHESIS:
            return self._run_synthesis(node, inputs)
        if kind is NodeKind.EVALUATION:
            return self._run_evaluation(node, inputs)
        if kind is NodeKind.KNOWLEDGE:
            return self._run_knowledge(node, inputs)
        if kind is NodeKind.OUTPUT:
            # The engine falls back to the raw inputs when OUTPUT returns {}.
            return dict(inputs)
        # INPUT / PARALLEL / JOIN / CONDITION are handled by the engine itself;
        # if one still reaches a runtime, pass its inputs straight through.
        return {_primary_output(node): _collect_text(inputs)}

    # ------------------------------------------------------------------ #
    # PIPELINE (real research/bbp node functions)
    # ------------------------------------------------------------------ #

    async def _run_pipeline_node(
        self, pipeline_node: str, node: GraphNode, inputs: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute a REAL workflow node function against the carried state.

        The pipeline's node functions take a :class:`WorkflowState` and return
        a :class:`NodeResult` of ``state_updates``. We keep the accumulating
        ``data`` dict here so consecutive nodes see each other's output, the
        same way the in-memory runner threads state.
        """
        from uap.workflows.runner import NodeResult  # local: avoid import cycle
        from uap.contracts.models import WorkflowState

        fn = getattr(self.pipeline, f"_{pipeline_node}", None)
        if fn is None:
            raise NodeExecutionError(
                f"pipeline node {pipeline_node!r} is not implemented on "
                f"{type(self.pipeline).__name__}"
            )

        # Seed the question or task data from graph input on the first node.
        if "task" not in self._pipeline_data and self.task is not None:
            self._pipeline_data["task"] = self.task.model_dump()
        if pipeline_node == "question_analysis":
            text = _collect_text(inputs).strip()
            if text:
                if "task" not in self._pipeline_data:
                    goal = self.task.goal if self.task is not None else text
                    self._pipeline_data["task"] = {
                        "goal": goal,
                        "input": {"question": text, "raw": text},
                    }
                else:
                    self._pipeline_data["task"].setdefault("input", {})["question"] = text
        elif pipeline_node == "scope_validation":
            if self.task is not None:
                self._pipeline_data["task"] = self.task.model_dump()
        if "task_id" not in self._pipeline_data and self.task is not None:
            self._pipeline_data["task_id"] = self.task.task_id

        workflow_name = getattr(self.pipeline, "workflow_name", getattr(self.pipeline, "name", "research"))
        state = WorkflowState(
            task_id=str(self._pipeline_data.get("task_id", "")),
            workflow=str(workflow_name),
            data=dict(self._pipeline_data),
        )
        is_agent_ish = (
            pipeline_node in ("question_analysis", "research_planning", "synthesis")
            or node.kind in (NodeKind.AGENT, NodeKind.SYNTHESIS)
        )
        if is_agent_ish:
            compiled_ctx = self.compile_context(node=node, inputs=inputs, current_node=pipeline_node)
            self._latest_context = compiled_ctx
            if pipeline_node == "synthesis":
                self._wrap_synthesizer_if_present()

        result = await fn(state)
        if isinstance(result, NodeResult):
            self._pipeline_data.update(result.state_updates or {})
        elif isinstance(result, dict):
            self._pipeline_data.update(result)

        # Surface the node's most useful product as the port value.
        produced: Any = None
        for key in ("output", "synthesis", "report", "question", "plan", "evidence"):
            if key in (result.state_updates if isinstance(result, NodeResult) else {}) or (
                isinstance(result, dict) and key in result
            ):
                produced = (
                    result.state_updates.get(key)
                    if isinstance(result, NodeResult)
                    else result.get(key)
                )
                if produced:
                    break
        if produced is None:
            produced = _collect_text(inputs)
        port_values = {port: {"content": produced} for port in _output_port_names(node)}
        if is_agent_ish:
            inspectable = self.get_inspectable_context(pipeline_node)
            for v in port_values.values():
                v["context"] = inspectable
        return port_values

    # ------------------------------------------------------------------ #
    # AGENT
    # ------------------------------------------------------------------ #

    async def _run_agent(
        self, node: GraphNode, inputs: dict[str, Any], ctx: ExecutionContext
    ) -> dict[str, Any]:
        # Agent name: config["agent"] | config["agent_ref"] | version_ref | title.
        ref = (
            node.config.get("agent")
            or node.config.get("agent_ref")
            or node.version_ref
            or node.title
        )
        if not ref:
            raise NodeExecutionError(
                f"AGENT node {node.id!r} has no agent reference in config/version_ref"
            )
        name = str(ref).split("@", 1)[0].split(":", 1)[-1]
        agent = self.agents.get(name)
        if agent is None:
            raise NodeExecutionError(
                f"AGENT node {node.id!r}: no agent registered as {name!r} "
                f"(known: {self.agents.names()})"
            )

        message = _collect_text(inputs)
        task = self.task or ctx.metadata.get("task")
        if not isinstance(task, TaskSpec):
            task = TaskSpec(
                domain="research",
                goal=node.title or f"run {name}",
                input={"raw": message, "question": message},
            )
        context = self.compile_context(node=node, inputs=inputs, task=task, current_node=node.id)
        if message:
            context.extras["message"] = message
        result = await agent.run(context)
        if result.status is AgentStatus.FAILED:
            raise NodeExecutionError(
                f"AGENT node {node.id!r}: agent {name!r} failed: "
                f"{result.error or 'no error detail'}"
            )
        inspectable = self.get_inspectable_context(node.id)
        value = {
            "content": result.output,
            "agent": name,
            "status": result.status.value,
            "context": inspectable,
        }
        return {port: value for port in _output_port_names(node)}

    # ------------------------------------------------------------------ #
    # TOOL
    # ------------------------------------------------------------------ #

    async def _run_tool(
        self, node: GraphNode, inputs: dict[str, Any]
    ) -> dict[str, Any]:
        ref = node.config.get("tool") or node.config.get("tool_ref") or node.version_ref
        if not ref:
            raise NodeExecutionError(
                f"TOOL node {node.id!r} has no tool reference in config/version_ref"
            )
        name = str(ref).split("@", 1)[0].split(":", 1)[-1]
        # Args: explicit config['args'] wins, else the inbound ports verbatim.
        args = node.config.get("args")
        if not isinstance(args, dict):
            args = {k: v for k, v in inputs.items()}
        result = await self.tools.call(name, args)
        if not result.ok:
            raise NodeExecutionError(
                f"TOOL node {node.id!r}: tool {name!r} denied/failed: "
                f"{result.error or 'no error detail'}"
            )
        value = {"content": result.result, "tool": name}
        return {port: value for port in _output_port_names(node)}

    # ------------------------------------------------------------------ #
    # Deterministic built-ins
    # ------------------------------------------------------------------ #

    def _run_synthesis(
        self, node: GraphNode, inputs: dict[str, Any]
    ) -> dict[str, Any]:
        combined = _collect_text(inputs)
        value = {"content": combined, "synthesized": True}
        return {port: value for port in _output_port_names(node)}

    def _run_evaluation(
        self, node: GraphNode, inputs: dict[str, Any]
    ) -> dict[str, Any]:
        combined = _collect_text(inputs)
        passed = bool(combined.strip())
        value = {
            "content": combined,
            "passed": passed,
            "evidence": f"content length {len(combined)}",
        }
        return {port: value for port in _output_port_names(node)}

    def _run_knowledge(
        self, node: GraphNode, inputs: dict[str, Any]
    ) -> dict[str, Any]:
        combined = _collect_text(inputs)
        # First sentence makes a reasonable single-claim statement.
        statement = combined.strip().split("\n", 1)[0][:280] or "no content"
        value = {
            "content": combined,
            "statement": statement,
            "knowledge_candidate": True,
        }
        return {port: value for port in _output_port_names(node)}

    # ------------------------------------------------------------------ #
    # Context compilation & inspectability
    # ------------------------------------------------------------------ #

    def _wrap_synthesizer_if_present(self) -> None:
        synth = getattr(self.pipeline, "synthesizer", None)
        if synth is not None and not getattr(synth, "_context_wrapped", False):
            orig = synth

            async def _synthesize(question: str, verified: list[dict[str, Any]], *args: Any, **kwargs: Any) -> str:
                ctx = kwargs.get("context") or self._latest_context or self.get_compiled_context()
                try:
                    return await orig(question, verified, context=ctx)
                except TypeError:
                    return await orig(question, verified)

            _synthesize._context_wrapped = True  # type: ignore[attr-defined]
            self.pipeline.synthesizer = _synthesize

    def _retrieve_knowledge(self, query: str) -> list[dict[str, Any]]:
        store = self.knowledge_store
        if store is not None:
            return self._extract_knowledge_items(store, query)
        if self.session_factory is not None:
            try:
                from uap.db.engine import session_scope
                from uap.knowledge.store import KnowledgeStore

                with session_scope(self.session_factory) as session:
                    store = KnowledgeStore(session)
                    return self._extract_knowledge_items(store, query)
            except Exception as exc:
                log_swallowed_exception(
                    logger,
                    exc,
                    "failed to retrieve knowledge items from database",
                    level=logging.WARNING,
                    query=query[:60],
                )
                return []
        return []

    def _extract_knowledge_items(self, store: Any, query: str) -> list[dict[str, Any]]:
        items: list[Any] = []
        if hasattr(store, "search"):
            try:
                res = store.search(query)
                items = [r[0] if isinstance(r, tuple) else r for r in res]
            except TypeError:
                try:
                    from uap.knowledge.embeddings import get_embedder

                    res = store.search(query, embedder=get_embedder())
                    items = [r[0] if isinstance(r, tuple) else r for r in res]
                except Exception as exc:
                    log_swallowed_exception(
                        logger,
                        exc,
                        "knowledge search with embedder failed",
                        level=logging.DEBUG,
                    )
            except Exception as exc:
                log_swallowed_exception(
                    logger,
                    exc,
                    "knowledge store search failed",
                    level=logging.DEBUG,
                )
        if not items and hasattr(store, "list_by_status"):
            try:
                from uap.knowledge.model import KnowledgeStatus

                for status in (KnowledgeStatus.PROMOTED, KnowledgeStatus.VERIFIED, KnowledgeStatus.PROPOSED):
                    found = store.list_by_status(status)
                    if found:
                        items.extend(found)
                        break
            except Exception as exc:
                log_swallowed_exception(
                    logger,
                    exc,
                    "knowledge list_by_status failed",
                    level=logging.DEBUG,
                )
        if not items:
            for attr in ("list_all", "get_all", "list", "items"):
                val = getattr(store, attr, None)
                if callable(val):
                    try:
                        items = list(val())
                        if items:
                            break
                    except Exception as exc:
                        log_swallowed_exception(
                            logger,
                            exc,
                            f"knowledge store {attr} call failed",
                            level=logging.DEBUG,
                        )
                elif isinstance(val, (list, tuple)):
                    items = list(val)
                    if items:
                        break

        out: list[dict[str, Any]] = []
        for it in items:
            if isinstance(it, dict):
                key = it.get("key") or it.get("knowledge_id") or "item"
                statement = it.get("content") or it.get("statement") or ""
                prov = it.get("provenance") or []
                score = it.get("score") or it.get("confidence") or 0.5
                tags = it.get("tags") or []
            else:
                key = getattr(it, "knowledge_id", "item")
                statement = getattr(it, "statement", "")
                prov = getattr(it, "provenance", []) or []
                score = getattr(it, "confidence", 0.5)
                tags = getattr(it, "tags", []) or []

            prov_refs: list[str] = []
            for p in prov:
                if isinstance(p, dict):
                    k = p.get("source_kind", "source")
                    r = p.get("source_ref", "")
                    prov_refs.append(f"{k}:{r}" if r else str(k))
                elif hasattr(p, "source_kind") and hasattr(p, "source_ref"):
                    prov_refs.append(f"{p.source_kind}:{p.source_ref}")
                else:
                    prov_refs.append(str(p))

            prov_str = ", ".join(prov_refs)
            content = statement
            if prov_str:
                content = f"{statement} [provenance: {prov_str}]"

            out.append(
                {
                    "key": str(key),
                    "content": content,
                    "score": float(score) if score is not None else 0.5,
                    "tags": list(tags),
                    "provenance": prov_refs,
                    "source_ref": prov_str or f"knowledge:{key}",
                }
            )
        return out

    def compile_context(
        self,
        node: GraphNode | None = None,
        inputs: dict[str, Any] | None = None,
        *,
        task: TaskSpec | None = None,
        current_node: str | None = None,
    ) -> AgentContext:
        inputs = inputs or {}
        node_name = current_node or (node.id if node else "node")
        effective_task = task or self.task
        question = ""
        if isinstance(self._pipeline_data.get("task"), dict):
            t = self._pipeline_data["task"]
            question = str(t.get("input", {}).get("question") or t.get("goal") or "")
        if not question and "question" in self._pipeline_data:
            question = str(self._pipeline_data["question"])
        if not question and effective_task and effective_task.goal:
            question = effective_task.goal
        if not question:
            question = _collect_text(inputs).strip()

        if not isinstance(effective_task, TaskSpec):
            effective_task = TaskSpec(
                domain="research",
                goal=question or (node.title if node else f"run {node_name}"),
                input={"question": question, "raw": question},
            )
        elif question and "question" not in (effective_task.input or {}):
            inputs_dict = dict(effective_task.input or {})
            inputs_dict["question"] = question
            effective_task = effective_task.model_copy(update={"input": inputs_dict})

        prior_outputs: dict[str, Any] = {}
        for k, v in self._pipeline_data.items():
            if k.startswith("_") or k in ("task", "task_id"):
                continue
            prior_outputs[k] = v
        inbound = _collect_text(inputs).strip()
        if inbound and "input" not in prior_outputs:
            prior_outputs["input"] = inbound

        knowledge_items = self._retrieve_knowledge(question)
        task_id = str(self._pipeline_data.get("task_id", getattr(effective_task, "task_id", "")))
        task_state = WorkflowState(
            task_id=task_id,
            workflow=getattr(self.pipeline, "name", "research") if self.pipeline else "research",
            current_node=node_name,
            data=filter_secrets(dict(self._pipeline_data)),
        )

        compiled = self.compiler.compile(
            effective_task,
            task_state=task_state,
            prior_outputs=prior_outputs,
            knowledge=knowledge_items,
        )
        self._compiled_contexts[node_name] = compiled
        if node and node.id:
            self._compiled_contexts[node.id] = compiled
        self._latest_context = compiled
        return compiled

    def get_inspectable_context(self, node_id: str | None = None) -> dict[str, Any]:
        """Return inspectable view of compiled context (keys + sizes + source refs, no secret content)."""
        ctx: AgentContext | None = None
        if node_id and node_id in self._compiled_contexts:
            ctx = self._compiled_contexts[node_id]
        elif self._latest_context is not None:
            ctx = self._latest_context
        elif self._compiled_contexts:
            ctx = list(self._compiled_contexts.values())[-1]

        if ctx is None:
            return {"sections": [], "total_chars": 0, "budget": {}}

        sensitive = ("secret", "token", "password", "credential", "api_key", "auth", "privkey")
        source_map = {}
        for c in getattr(self.compiler, "last_selected", []):
            source_map[c.key] = c.source_ref or c.source

        sections: list[dict[str, Any]] = []
        for s in ctx.context_sections:
            if any(sens in s.key.lower() for sens in sensitive):
                continue
            src_ref = source_map.get(s.key)
            if not src_ref:
                if s.key.startswith("knowledge:"):
                    src_ref = s.key
                elif s.key.startswith("prior:"):
                    src_ref = f"prior_output:{s.key.split(':', 1)[-1]}"
                elif s.key == "task_state":
                    src_ref = "task_state"
                else:
                    src_ref = s.key

            sections.append(
                {
                    "key": s.key,
                    "size": len(s.content),
                    "source_ref": src_ref,
                }
            )
        return {
            "task_id": ctx.task.task_id if ctx.task else "",
            "sections": sections,
            "total_chars": sum(s["size"] for s in sections),
            "budget": ctx.budget,
            "extras": {
                "selected_count": len(sections),
                "dropped_count": ctx.extras.get("dropped_count", 0),
            },
        }

    @property
    def pipeline_artifacts(self) -> list[Any]:
        """Artifacts emitted by pipeline nodes into state_updates['artifacts']."""
        raw = self._pipeline_data.get("artifacts") or []
        from uap.contracts.models import Artifact

        return [a for a in raw if isinstance(a, Artifact)]
