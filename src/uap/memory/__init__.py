"""Memory + User Model (Master sections 15, 16, 21, 29).

Memory is durable knowledge separated from runtime/checkpoint state. This
package provides three things:

* :class:`MemoryStore` - SQLite persistence for scored, expirable memories.
* :class:`MemoryExtractor` - deterministic proposal of memory candidates after
  a workflow finishes. It proposes; lifecycle rules decide (nothing is stored
  automatically).
* :class:`UserModelStore` - SQLite persistence for preferences, skill levels,
  and evidence-backed observations.
"""

from uap.memory.extractor import MemoryExtractor
from uap.memory.store import MemoryStore
from uap.memory.user_model import UserModelStore

__all__ = ["MemoryStore", "MemoryExtractor", "UserModelStore"]
