"""Global Model Router & budget policy (Master sections 33, 34).

Provider-agnostic model selection, layered budget policy and usage accounting.
The whole package is deterministic and offline: the catalog is data, routing is
pure ranking, and no provider SDK or network call is involved.

Public API::

    from uap.models import (
        ModelCapability, ModelInfo, ModelCatalog,
        BudgetPolicy, PolicyResolver,
        RoutingRequest, RoutingDecision, RoutingError, ModelRouter,
        BudgetTracker, BudgetExceededError,
    )
"""

from uap.models.budget import BudgetExceededError, BudgetTracker
from uap.models.catalog import ModelCapability, ModelCatalog, ModelInfo
from uap.models.policy import BudgetPolicy, PolicyResolver
from uap.models.router import (
    ModelRouter,
    RoutingDecision,
    RoutingError,
    RoutingRequest,
)

__all__ = [
    "ModelCapability",
    "ModelInfo",
    "ModelCatalog",
    "BudgetPolicy",
    "PolicyResolver",
    "RoutingRequest",
    "RoutingDecision",
    "RoutingError",
    "ModelRouter",
    "BudgetTracker",
    "BudgetExceededError",
]
