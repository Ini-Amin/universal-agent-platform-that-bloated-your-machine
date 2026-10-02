"""Port type system for the canonical graph (Master section 18).

Section 18 requires *compatible* types to connect and incompatible connections
to be blocked (or to suggest an adapter). This module owns the single,
documented compatibility table used by the validator, the UI and the AI
workflow generator, so every consumer agrees on what may connect.

Compatibility is **directional**, not symmetric: an upcast is allowed in the
lossless direction only. ``TEXT`` may become ``JSON`` (wrap the string), but
``JSON`` may not silently become ``TEXT`` except through the explicit
"stringify" rule below, which is a lossy-but-sanctioned projection.

    source \\ target   ANY   TEXT   JSON   EVIDENCE   ARTIFACT   CONTROL
    ANY                yes   yes    yes    yes        yes        yes
    TEXT               yes   yes    yes    no         no         no
    JSON               yes   yes    yes    no         no         no
    EVIDENCE           yes   no     yes    yes        no         no
    ARTIFACT           yes   no     yes    no         yes        no
    CONTROL            yes   no     no     no         no         yes

Rules, in order:

1. ``ANY`` on either side is compatible with everything (untyped escape hatch).
2. Identical types are compatible.
3. ``TEXT -> JSON``    : lossless wrap of a string as a JSON scalar.
4. ``EVIDENCE -> JSON``: evidence records are JSON-serializable.
5. ``ARTIFACT -> JSON``: artifact manifests are JSON-serializable.
6. ``JSON -> TEXT``    : sanctioned stringify (lossy, but explicit).
7. Anything else requires an exact match and is otherwise incompatible.
"""

from __future__ import annotations

from .model import PortType

__all__ = ["port_type_compatible", "type_compatibility_matrix"]

# Directed upcasts that are allowed without an adapter node (rules 3-6).
_LOSSLESS_UPCASTS: frozenset[tuple[PortType, PortType]] = frozenset(
    {
        (PortType.TEXT, PortType.JSON),
        (PortType.EVIDENCE, PortType.JSON),
        (PortType.ARTIFACT, PortType.JSON),
        (PortType.JSON, PortType.TEXT),
    }
)


def port_type_compatible(source: PortType, target: PortType) -> bool:
    """Return whether a value of ``source`` may flow into a ``target`` port.

    See the module docstring for the full, directional table. ``ANY`` is
    compatible on either side; exact matches and the four documented upcasts are
    allowed; every other pair is rejected.
    """
    if source is PortType.ANY or target is PortType.ANY:
        return True
    if source is target:
        return True
    return (source, target) in _LOSSLESS_UPCASTS


def type_compatibility_matrix() -> dict[str, bool]:
    """Flat ``"source->target" -> bool`` matrix for UI/AI consumption.

    Always contains ``len(PortType) ** 2`` entries (one per ordered pair), so a
    UI can render the full grid and an AI generator can look up any pair without
    special-casing missing keys.
    """
    return {
        f"{source.value}->{target.value}": port_type_compatible(source, target)
        for source in PortType
        for target in PortType
    }
