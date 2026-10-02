"""Closed-loop learning (Master section 48).

Public API::

    from uap.learning import (
        StrategyProposal,
        ProposalKind,
        LearningLoop,
    )

    loop = LearningLoop(evaluator_runner, min_failures_for_proposal=2)
    observation = loop.observe(records)
    proposals = loop.propose(observation, target_ref="workflow:recon@v1")
    approved = loop.approve(proposals[0], decided_by="alice")
    applied = loop.apply(approved, applier=my_versioning_fn)
"""

from .loop import LearningLoop
from .proposal import ProposalKind, ProposalStatus, StrategyProposal

__all__ = [
    "LearningLoop",
    "ProposalKind",
    "ProposalStatus",
    "StrategyProposal",
]
