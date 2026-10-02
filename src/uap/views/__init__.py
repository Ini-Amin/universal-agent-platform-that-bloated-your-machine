"""Node views — the visible product of a node's work (canvas-as-stage).

Public surface:

* :class:`~uap.views.store.NodeViewStore` — durable per-``(execution, node)``
  view persistence.
* :data:`~uap.views.store.NODE_VIEW_EVENT_KIND` — the event kind mirrored when a
  node publishes a view.
"""

from __future__ import annotations

from .store import NODE_VIEW_EVENT_KIND, NodeViewStore

__all__ = ["NodeViewStore", "NODE_VIEW_EVENT_KIND"]
