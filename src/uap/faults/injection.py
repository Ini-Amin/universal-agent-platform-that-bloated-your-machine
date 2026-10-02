"""Fault injection (Master section 59).

Deliberately fail parts of an execution to prove recovery behavior (section 59:
"Test failures deliberately ... demonstrate recovery behavior"). A
:class:`FaultInjector` holds a list of :class:`FaultSpec` rules; :meth:`apply`
wraps an async callable and fires the matching fault instead of (or around) the
real call.

:class:`FaultRegistry` adapts an injector into a :class:`NodeRuntime` decorator
so the committed :class:`~uap.execution.GraphExecutor` can be driven with faults
on specific nodes - without touching the engine.

Everything is deterministic: ``trigger_after`` counts matching calls per spec,
so a test gets the same fault on the same call every run.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["FaultSpec", "FaultInjector", "FaultRegistry"]

FaultKind = Literal["exception", "delay", "result_mutation", "kill_after_n"]

# Exception type names the injector can raise. Restricted to builtins so a spec
# can never import arbitrary code (section 59 is a *testing* surface, not an
# arbitrary-code hook).
_EXCEPTION_TYPES: dict[str, type[BaseException]] = {
    "RuntimeError": RuntimeError,
    "TimeoutError": TimeoutError,
    "ConnectionError": ConnectionError,
    "ValueError": ValueError,
    "KeyError": KeyError,
    "MemoryError": MemoryError,
    "OSError": OSError,
}


class FaultSpec(BaseModel):
    """One fault rule targeting a node id (or ``"*"`` for every node)."""

    model_config = ConfigDict(extra="forbid")

    fault_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    #: Target node id, or ``"*"`` to match every node.
    target: str
    kind: FaultKind
    #: Name of the exception type to raise for ``kind="exception"``.
    exception_type: str = "RuntimeError"
    message: str = "injected fault"
    #: Seconds to await for ``kind="delay"``.
    delay_s: float = 0.0
    #: Fire on the Nth matching call (1-based). ``0`` fires on *every* call.
    trigger_after: int = 0


class FaultInjector:
    """Holds fault specs and applies the matching one around an async call."""

    def __init__(self, specs: list[FaultSpec] | None = None) -> None:
        self._specs: list[FaultSpec] = list(specs) if specs else []
        # Per-spec count of matching calls seen so far (deterministic trigger).
        self._hits: dict[str, int] = {}

    def add(self, spec: FaultSpec) -> None:
        """Register a fault spec."""

        self._specs.append(spec)

    def clear(self) -> None:
        """Remove every spec and reset the trigger counters."""

        self._specs.clear()
        self._hits.clear()

    def should_fire(self, target: str) -> FaultSpec | None:
        """Return the spec that fires for ``target`` on this call, or ``None``.

        Advances the per-spec hit counter for every spec whose target matches
        ``target`` (exact id or ``"*"``). A spec fires when ``trigger_after`` is
        ``0`` (every call) or when this is exactly its ``trigger_after``-th
        matching call. The first firing spec (declaration order) wins.
        """

        fired: FaultSpec | None = None
        for spec in self._specs:
            if spec.target != "*" and spec.target != target:
                continue
            count = self._hits.get(spec.fault_id, 0) + 1
            self._hits[spec.fault_id] = count
            if fired is None and (
                spec.trigger_after == 0 or count == spec.trigger_after
            ):
                fired = spec
        return fired

    async def apply(
        self,
        target: str,
        fn: Callable[..., Awaitable[Any]],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Run ``fn`` under any fault that fires for ``target``.

        * ``exception``       - raise the mapped exception type with ``message``
          *instead of* calling ``fn``.
        * ``delay``           - await ``delay_s`` then call ``fn`` normally.
        * ``result_mutation`` - call ``fn``, then stamp ``{"__injected__": id}``
          into a dict result (or append it to a list result).
        * ``kill_after_n``    - raise ``RuntimeError("worker killed by fault
          injection")`` *instead of* calling ``fn`` (chosen over ``SystemExit``
          so the engine's normal node-failure path handles it as a crash
          surrogate rather than tearing down the interpreter).
        """

        spec = self.should_fire(target)
        if spec is None:
            return await fn(*args, **kwargs)

        if spec.kind == "exception":
            raise self._build_exception(spec)

        if spec.kind == "kill_after_n":
            raise RuntimeError("worker killed by fault injection")

        if spec.kind == "delay":
            if spec.delay_s > 0:
                await asyncio.sleep(spec.delay_s)
            return await fn(*args, **kwargs)

        if spec.kind == "result_mutation":
            result = await fn(*args, **kwargs)
            return self._mutate(result, spec.fault_id)

        # Unreachable under the closed FaultKind literal, but explicit beats a
        # silent swallow (section 73).
        raise ValueError(f"unknown fault kind {spec.kind!r}")

    @staticmethod
    def _build_exception(spec: FaultSpec) -> BaseException:
        exc_type = _EXCEPTION_TYPES.get(spec.exception_type, RuntimeError)
        return exc_type(spec.message)

    @staticmethod
    def _mutate(result: Any, fault_id: str) -> Any:
        if isinstance(result, dict):
            mutated = dict(result)
            mutated["__injected__"] = fault_id
            return mutated
        if isinstance(result, list):
            return [*result, {"__injected__": fault_id}]
        return result


class FaultRegistry:
    """Adapts a :class:`FaultInjector` into a :class:`NodeRuntime` decorator.

    :meth:`wrap_node_runtime` returns a runtime whose ``run_node`` routes each
    node through the injector, so a committed :class:`GraphExecutor` run fails
    (or mutates) exactly the targeted nodes - integration without engine edits
    (section 59).
    """

    def __init__(self, injector: FaultInjector | None = None) -> None:
        self.injector = injector if injector is not None else FaultInjector()

    def add(self, spec: FaultSpec) -> None:
        """Register a fault spec on the underlying injector."""

        self.injector.add(spec)

    def clear(self) -> None:
        """Clear the underlying injector."""

        self.injector.clear()

    def wrap_node_runtime(self, runtime: Any) -> Any:
        """Return a :class:`NodeRuntime` wrapping ``runtime`` with the injector."""

        return _InjectingRuntime(runtime, self.injector)


class _InjectingRuntime:
    """A ``NodeRuntime`` that funnels each node through a :class:`FaultInjector`."""

    def __init__(self, inner: Any, injector: FaultInjector) -> None:
        self._inner = inner
        self._injector = injector

    async def run_node(self, node: Any, inputs: dict[str, Any], ctx: Any) -> dict[str, Any]:
        return await self._injector.apply(
            node.id, self._inner.run_node, node, inputs, ctx
        )
