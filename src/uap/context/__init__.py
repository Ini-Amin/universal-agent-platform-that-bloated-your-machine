"""Context package public API (Master section 15).

The Context Compiler selects only relevant context; it never dumps the entire
database, memory, knowledge base, or user history into a prompt.
"""

from uap.context.compiler import ContextBudget, ContextCompiler

__all__ = ["ContextBudget", "ContextCompiler"]
