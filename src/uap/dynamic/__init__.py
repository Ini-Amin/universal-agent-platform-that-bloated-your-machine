"""Dynamic graph mutation (Master section 20).

Runtime agents may reshape the graph *of a running execution* - add/remove
nodes and edges - but never the persistent workflow definition (section 2.3,
rule 10). Every mutation runs the section-20 pipeline:

    transaction -> validation -> policy (reason) -> audit -> checkpoint -> apply

:class:`GraphMutator.propose` operates transactionally on a *deep copy*: it
applies the change to the copy, validates it, and rejects on any error-severity
issue - the original graph is never touched. :meth:`GraphMutator.audit` records
the outcome as a ``REPLAN`` decision trace (section 26).
"""

from uap.dynamic.mutator import GraphMutation, GraphMutator, MutationResult

__all__ = ["GraphMutation", "GraphMutator", "MutationResult"]
