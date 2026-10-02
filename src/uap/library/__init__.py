"""Global Library: versioned, reusable definitions (Master sections 2.4 and 4).

The Library stores agents, tools, skills and workflows as immutable, explicitly
versioned documents. Consumers -- workspaces above all -- pin exact references
such as ``"recon-agent@v3"`` so publishing ``v4`` never silently changes them.

Public API::

    from uap.library import LibraryService

    library = LibraryService(session)
    library.register_agent("recon-agent", {"model": "..."})   # v1, draft
    library.publish(entry.definition_id, 1)                    # v1, active
    library.resolve_ref("recon-agent@v1")                      # exact
    library.resolve_ref("recon-agent")                         # latest active
"""

from .models import (
    SPEC_MODELS,
    AgentDefinitionSpec,
    SkillDefinitionSpec,
    ToolDefinitionSpec,
    WorkflowDefinitionSpec,
)
from .service import (
    KINDS,
    STATUS_ACTIVE,
    STATUS_DEPRECATED,
    STATUS_DRAFT,
    DuplicateDefinitionError,
    LibraryEntry,
    LibraryError,
    LibraryService,
    UnknownReferenceError,
)

__all__ = [
    "AgentDefinitionSpec",
    "DuplicateDefinitionError",
    "KINDS",
    "LibraryEntry",
    "LibraryError",
    "LibraryService",
    "SPEC_MODELS",
    "STATUS_ACTIVE",
    "STATUS_DEPRECATED",
    "STATUS_DRAFT",
    "SkillDefinitionSpec",
    "ToolDefinitionSpec",
    "UnknownReferenceError",
    "WorkflowDefinitionSpec",
]
