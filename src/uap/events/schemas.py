"""Per-event payload schemas for the canonical runtime event model (section 45).

:data:`PAYLOAD_SCHEMAS` maps every :class:`~uap.events.model.EventType` to a
``{"required": set[str], "optional": set[str]}`` description of its ``payload``.
:func:`uap.events.model.validate_event` reads it to produce errors (missing
required key) and warnings (unknown extra key) without ever raising.

The mapping is exhaustive: it covers *every* member of ``EventType`` (enforced
by a test), so adding a new event type without a schema is a loud failure.

Payloads are intentionally small and redaction-friendly: they carry summaries
and references (ids, names, counts, hashes) - never raw secrets or chain-of-
thought. Values under sensitive keys are scrubbed by
:func:`uap.trace.store.redact_secrets` before persistence.
"""

from __future__ import annotations

from uap.events.model import EventType

__all__ = ["PAYLOAD_SCHEMAS"]

#: One schema per event type. ``required`` keys must be present; ``optional``
#: keys may be present; anything else is reported as a warning.
PAYLOAD_SCHEMAS: dict[EventType, dict] = {
    # -- execution lifecycle ------------------------------------------------ #
    # Execution run began; ``workflow_version_id`` pins the exact definition.
    EventType.EXECUTION_STARTED: {
        "required": {"workflow_version_id"},
        "optional": {"input_summary", "mode", "autonomy"},
    },
    # Execution finished successfully; ``status`` is the terminal status.
    EventType.EXECUTION_COMPLETED: {
        "required": {"status"},
        "optional": {"duration_ms", "output_ref", "artifact_count"},
    },
    # Execution terminated with an error; ``error`` is a human-readable reason.
    EventType.EXECUTION_FAILED: {
        "required": {"error"},
        "optional": {"node_id", "error_type", "retryable"},
    },
    # Execution paused (human intervention / budget); ``reason`` explains why.
    EventType.EXECUTION_PAUSED: {
        "required": {"reason"},
        "optional": {"at_node_id", "resume_token"},
    },
    # Execution resumed from a checkpoint; ``resume_count`` is the new total.
    EventType.EXECUTION_RESUMED: {
        "required": {"resume_count"},
        "optional": {"from_checkpoint_id", "at_node_id"},
    },
    # A new execution was forked from this one; ``child_execution_id`` links it.
    EventType.EXECUTION_FORKED: {
        "required": {"child_execution_id"},
        "optional": {"from_node_id", "reason"},
    },

    # -- node lifecycle ----------------------------------------------------- #
    # A graph node began; ``node_type`` distinguishes agent/tool/control nodes.
    EventType.NODE_STARTED: {
        "required": {"node_type"},
        "optional": {"attempt", "input_summary"},
    },
    # A graph node finished; ``duration_ms`` records its wall time.
    EventType.NODE_FINISHED: {
        "required": {"duration_ms"},
        "optional": {"output_ref", "next_node_id", "attempt"},
    },
    # A graph node failed; ``error`` is the surfaced reason.
    EventType.NODE_FAILED: {
        "required": {"error"},
        "optional": {"error_type", "attempt", "retryable"},
    },
    # A graph node was skipped; ``reason`` is why (condition false, etc.).
    EventType.NODE_SKIPPED: {
        "required": {"reason"},
        "optional": {"condition_ref"},
    },

    # -- agents ------------------------------------------------------------- #
    # An agent was invoked; ``agent`` names it, ``model`` the chosen model.
    EventType.AGENT_INVOKED: {
        "required": {"agent"},
        "optional": {"model", "task_summary", "tool_count"},
    },
    # An agent completed; ``status`` is its terminal agent status.
    EventType.AGENT_COMPLETED: {
        "required": {"agent", "status"},
        "optional": {"duration_ms", "tool_calls", "artifact_count"},
    },

    # -- tools -------------------------------------------------------------- #
    # A tool was called; ``args_summary`` is a redacted argument digest.
    EventType.TOOL_CALLED: {
        "required": {"tool", "args_summary"},
        "optional": {"call_id", "risk_tier"},
    },
    # A tool returned; ``ok`` says whether it succeeded.
    EventType.TOOL_RESULT: {
        "required": {"tool", "ok"},
        "optional": {"call_id", "duration_ms", "result_ref", "error"},
    },
    # A tool call was denied by policy; ``reason`` names the policy rule.
    EventType.TOOL_DENIED: {
        "required": {"tool", "reason"},
        "optional": {"policy_ref", "risk_tier", "call_id"},
    },

    # -- model selection ---------------------------------------------------- #
    # A model was chosen; ``reason`` is the short selection rationale.
    EventType.MODEL_SELECTED: {
        "required": {"model", "reason"},
        "optional": {"agent", "candidates", "cost_estimate"},
    },
    # A fallback model was used; ``from_model`` -> ``model`` records the switch.
    EventType.MODEL_FALLBACK: {
        "required": {"from_model", "model", "reason"},
        "optional": {"attempt", "error_type"},
    },

    # -- decision trace (section 26) ---------------------------------------- #
    # A structured decision was recorded; links to the trace row by id.
    EventType.DECISION_RECORDED: {
        "required": {"decision_type", "chosen", "trace_id"},
        "optional": {"rationale", "confidence", "alternative_count", "evidence_count"},
    },

    # -- approval ----------------------------------------------------------- #
    # Human approval was requested; ``action`` describes the gated action.
    EventType.APPROVAL_REQUESTED: {
        "required": {"approval_id", "action"},
        "optional": {"risk_tier", "requested_by", "details_summary"},
    },
    # A human approval was decided; ``decision`` is granted/denied.
    EventType.APPROVAL_DECIDED: {
        "required": {"approval_id", "decision"},
        "optional": {"decided_by", "notes_summary"},
    },

    # -- checkpointing ------------------------------------------------------ #
    # A checkpoint was saved; ``seq`` is the per-execution checkpoint number.
    EventType.CHECKPOINT_SAVED: {
        "required": {"checkpoint_id", "seq"},
        "optional": {"node_id", "state_size"},
    },
    # A checkpoint was restored; ``seq`` is the checkpoint that was loaded.
    EventType.CHECKPOINT_RESTORED: {
        "required": {"checkpoint_id", "seq"},
        "optional": {"node_id", "reason"},
    },

    # -- artifacts ---------------------------------------------------------- #
    # An artifact was produced; ``artifact_id``/``type`` identify it.
    EventType.ARTIFACT_CREATED: {
        "required": {"artifact_id", "type"},
        "optional": {"version", "uri", "content_hash"},
    },

    # -- knowledge / learning ----------------------------------------------- #
    # Knowledge was proposed (not yet trusted); ``proposal_id`` identifies it.
    EventType.KNOWLEDGE_PROPOSED: {
        "required": {"proposal_id", "summary"},
        "optional": {"source", "confidence"},
    },
    # A knowledge proposal was promoted to durable knowledge.
    EventType.KNOWLEDGE_PROMOTED: {
        "required": {"proposal_id"},
        "optional": {"knowledge_id", "approved_by", "version"},
    },

    # -- evaluation --------------------------------------------------------- #
    # An evaluation run began; ``suite`` names the benchmark/dataset.
    EventType.EVALUATION_STARTED: {
        "required": {"suite"},
        "optional": {"case_count", "target_ref"},
    },
    # An evaluation run completed; ``score`` is the aggregate metric.
    EventType.EVALUATION_COMPLETED: {
        "required": {"suite", "score"},
        "optional": {"passed", "case_count", "duration_ms"},
    },

    # -- generic ------------------------------------------------------------ #
    # A generic error; ``message`` is the surfaced description.
    EventType.ERROR: {
        "required": {"message"},
        "optional": {"error_type", "component", "retryable"},
    },
    # A retry attempt; ``attempt`` is the 1-based retry number.
    EventType.RETRY: {
        "required": {"attempt", "reason"},
        "optional": {"component", "max_attempts", "delay_ms"},
    },
}
