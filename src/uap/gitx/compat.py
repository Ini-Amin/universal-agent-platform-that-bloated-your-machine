"""Compatibility checks and migration proposals (Master sections 2.4, 51, 63).

Section 51 is explicit about the flow::

    Compatibility Check -> Incompatible -> Migration Proposal
        -> User / Policy Approval -> New Version

Nothing here executes a migration. The checker *reports*; the caller (a human or
a policy gate) decides. A mismatch is never silently resolved by swapping a
referenced version for a newer one.

Two surfaces are covered today:

* :meth:`CompatibilityChecker.check_definition` - a Library definition spec,
  validated against the schema version the running platform understands.
* :meth:`CompatibilityChecker.check_graph` - a canonical :class:`WorkflowGraph`,
  including its exact ``version_ref`` pins (``name@vN``).

Reports are plain pydantic models so they can be stored, logged or attached to a
definition without conversion.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from uap.graph.model import WorkflowGraph
from uap.library.models import SPEC_MODELS

__all__ = [
    "CompatibilityIssue",
    "CompatibilityReport",
    "CompatibilityChecker",
]

# An exact Library pin: "name@vN". The name may not be empty and may not itself
# contain '@', whitespace or a version-shaped suffix.
_EXACT_VERSION_REF = re.compile(r"^[^@\s]+@v\d+$")

_FORBID = ConfigDict(extra="forbid")


class CompatibilityIssue(BaseModel):
    """One problem found by a compatibility check."""

    model_config = _FORBID

    severity: Literal["error", "warning"]
    code: str
    message: str


class CompatibilityReport(BaseModel):
    """The outcome of a compatibility check.

    ``ok`` is true only when there are no ``error`` issues; ``warning`` issues
    never block, but are always surfaced so they can be acknowledged.
    """

    model_config = _FORBID

    ok: bool
    issues: list[CompatibilityIssue] = Field(default_factory=list)

    @property
    def errors(self) -> list[CompatibilityIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[CompatibilityIssue]:
        return [i for i in self.issues if i.severity == "warning"]


def _report(issues: list[CompatibilityIssue]) -> CompatibilityReport:
    return CompatibilityReport(
        ok=not any(i.severity == "error" for i in issues),
        issues=issues,
    )


class CompatibilityChecker:
    """Check definitions and graphs against the running platform's schema."""

    def __init__(self, *, current_schema_version: int = 1) -> None:
        if int(current_schema_version) < 1:
            raise ValueError("current_schema_version must be >= 1")
        self.current_schema_version = int(current_schema_version)

    # ------------------------------------------------------------------ #
    # Definitions
    # ------------------------------------------------------------------ #

    def check_definition(
        self,
        kind: str,
        spec: dict,
        *,
        required_schema_version: int | None = None,
    ) -> CompatibilityReport:
        """Check a definition spec for schema compatibility.

        Rules:

        * Unknown ``kind`` -> ``error`` (the platform cannot validate it).
        * ``required_schema_version`` given but the spec carries no
          ``schema_version`` -> ``warning`` (assume v1, flag for review).
        * ``spec["schema_version"]`` newer than the platform -> ``error`` whose
          message says a migration is required (section 51/63).
        * ``spec["schema_version"]`` older than the platform -> ``warning``
          (readable, but should be migrated when convenient).
        """
        issues: list[CompatibilityIssue] = []
        normalized = str(kind).strip().lower()

        if normalized not in SPEC_MODELS:
            issues.append(
                CompatibilityIssue(
                    severity="error",
                    code="unknown_kind",
                    message=(
                        f"unknown definition kind {kind!r}; known kinds are "
                        f"{sorted(SPEC_MODELS)}"
                    ),
                )
            )
            return _report(issues)

        if not isinstance(spec, dict):
            issues.append(
                CompatibilityIssue(
                    severity="error",
                    code="invalid_spec",
                    message="definition spec must be a dict",
                )
            )
            return _report(issues)

        raw_version = spec.get("schema_version")
        if raw_version is None:
            if required_schema_version is not None:
                issues.append(
                    CompatibilityIssue(
                        severity="warning",
                        code="missing_schema_version",
                        message=(
                            f"{normalized} spec declares no schema_version while "
                            f"v{required_schema_version} is required; assuming v1"
                        ),
                    )
                )
            return _report(issues)

        if not isinstance(raw_version, int) or isinstance(raw_version, bool):
            issues.append(
                CompatibilityIssue(
                    severity="error",
                    code="invalid_schema_version",
                    message=(
                        f"{normalized} spec has a non-integer schema_version "
                        f"({raw_version!r})"
                    ),
                )
            )
            return _report(issues)

        if raw_version > self.current_schema_version:
            issues.append(
                CompatibilityIssue(
                    severity="error",
                    code="migration_required",
                    message=(
                        f"{normalized} spec requires schema v{raw_version} but this "
                        f"platform supports v{self.current_schema_version}; a "
                        f"migration is required before it can be loaded"
                    ),
                )
            )
        elif raw_version < self.current_schema_version:
            issues.append(
                CompatibilityIssue(
                    severity="warning",
                    code="outdated_schema_version",
                    message=(
                        f"{normalized} spec is schema v{raw_version}, older than "
                        f"v{self.current_schema_version}; consider migrating"
                    ),
                )
            )
        return _report(issues)

    # ------------------------------------------------------------------ #
    # Graphs
    # ------------------------------------------------------------------ #

    def check_graph(self, graph: WorkflowGraph) -> CompatibilityReport:
        """Check a workflow graph's schema version and its version pins.

        Every ``version_ref`` must be an *exact* pin of the form ``name@vN``.
        A floating or malformed pin is an ``error``: section 2.4 forbids a
        definition silently changing under a consumer.
        """
        issues: list[CompatibilityIssue] = []

        if graph.schema_version > self.current_schema_version:
            issues.append(
                CompatibilityIssue(
                    severity="error",
                    code="migration_required",
                    message=(
                        f"graph schema v{graph.schema_version} is newer than the "
                        f"supported v{self.current_schema_version}; a migration is "
                        f"required"
                    ),
                )
            )
        elif graph.schema_version < self.current_schema_version:
            issues.append(
                CompatibilityIssue(
                    severity="warning",
                    code="outdated_schema_version",
                    message=(
                        f"graph schema v{graph.schema_version} is older than "
                        f"v{self.current_schema_version}; consider migrating"
                    ),
                )
            )

        for node in graph.nodes:
            ref = node.version_ref
            if ref is None:
                continue
            if not _EXACT_VERSION_REF.match(ref):
                issues.append(
                    CompatibilityIssue(
                        severity="error",
                        code="non_exact_version_ref",
                        message=(
                            f"node {node.id!r} pins {ref!r}, which is not an exact "
                            f"'name@vN' reference; floating or malformed pins are "
                            f"not allowed (section 2.4)"
                        ),
                    )
                )
        return _report(issues)

    # ------------------------------------------------------------------ #
    # Migration proposal
    # ------------------------------------------------------------------ #

    def propose_migration(self, report: CompatibilityReport) -> str:
        """Render a human-readable migration proposal. Never executed.

        The proposal lists every issue code so a reviewer can see exactly what
        would have to change, and states plainly that approval is required
        (section 51: mismatch -> proposal -> approval -> new version).
        """
        lines: list[str] = [
            "Compatibility Migration Proposal (Master section 51)",
            "=" * 48,
        ]
        if report.ok and not report.issues:
            lines.append("")
            lines.append("No compatibility issues found; no migration required.")
            return "\n".join(lines)

        lines.append("")
        lines.append(
            f"Result: {'OK (warnings only)' if report.ok else 'INCOMPATIBLE'}"
        )
        lines.append(
            f"Issues: {len(report.issues)} "
            f"({len(report.errors)} error, {len(report.warnings)} warning)"
        )
        lines.append("")
        lines.append("Issues to resolve:")
        for issue in report.issues:
            lines.append(f"  - [{issue.severity}] {issue.code}: {issue.message}")

        lines.append("")
        lines.append("Proposed migration steps (NOT executed):")
        lines.append(
            "  1. Create a new version of each affected definition that targets "
            f"schema v{self.current_schema_version}."
        )
        lines.append(
            "  2. Re-pin every referencing workflow to the new exact "
            "'name@vN' reference; leave old versions untouched (section 2.4)."
        )
        lines.append(
            "  3. Record the change through Git so the AI/human actor is visible "
            "in history (section 49)."
        )
        lines.append(
            "  4. Obtain user or policy approval before applying (section 51)."
        )
        lines.append("")
        lines.append(
            "Status: PROPOSAL ONLY - awaiting user/policy approval; nothing was "
            "modified."
        )
        return "\n".join(lines)
