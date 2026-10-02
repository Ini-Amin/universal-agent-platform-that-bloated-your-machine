"""ORM models for the UAP persistence layer (Master sections 42, 67).

Importing this package registers every table on :data:`uap.db.base.Base`
metadata, which is what Alembic's ``target_metadata`` points at.
"""

from __future__ import annotations

from uap.db.models.checkpoint import ExecutionCheckpoint
from uap.db.models.definitions import (
    AgentDefinition,
    AgentVersion,
    SkillDefinition,
    SkillVersion,
    ToolDefinition,
    ToolVersion,
    VersionStatus,
    WorkflowDefinition,
    WorkflowVersion,
)
from uap.db.models.execution import Execution, ExecutionEvent, ExecutionStatus
from uap.db.models.knowledge import (
    KnowledgeEventRow,
    KnowledgeItemRow,
    KnowledgeProvenanceRow,
)
from uap.db.models.trace import DecisionTraceRow
from uap.db.models.workspace import WorkspaceRow

__all__ = [
    "AgentDefinition",
    "AgentVersion",
    "DecisionTraceRow",
    "Execution",
    "ExecutionCheckpoint",
    "ExecutionEvent",
    "ExecutionStatus",
    "KnowledgeEventRow",
    "KnowledgeItemRow",
    "KnowledgeProvenanceRow",
    "SkillDefinition",
    "SkillVersion",
    "ToolDefinition",
    "ToolVersion",
    "VersionStatus",
    "WorkflowDefinition",
    "WorkflowVersion",
    "WorkspaceRow",
]
