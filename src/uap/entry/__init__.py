"""Entry Workflow: the universal front door (Master sections 3 and 4).

Usage::

    from uap.entry import EntryWorkflow, EntryOutcome

    workflow = EntryWorkflow()
    outcome = workflow.run(request)
    if outcome.needs_clarification:
        ask(outcome.question)
    else:
        hand_to_router(outcome.spec)
"""

from .stages import (
    DeterministicIntentAnalyzer,
    IntentResult,
)
from .workflow import EntryOutcome, EntryWorkflow, IntentAnalyzer

__all__ = [
    "EntryWorkflow",
    "EntryOutcome",
    "IntentAnalyzer",
    "IntentResult",
    "DeterministicIntentAnalyzer",
]
