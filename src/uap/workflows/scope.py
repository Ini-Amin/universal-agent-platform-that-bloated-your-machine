"""Deterministic scope gate for the BBP workflow (Master section 9).

Policy lives *outside* LLM reasoning: a target is probed only after this pure,
side-effect-free gate allows it. The semantics mirror the ``bugbounty-mcp``
scope engine documented in ``docs/research/bugbounty-mcp-inventory.md`` section
3:

1. out-of-scope rules have strict precedence over in-scope rules;
2. an empty in-scope list blocks everything (fail closed);
3. ``*.domain.com`` matches the root domain and any subdomain; every other
   pattern is an exact, case-insensitive host match.

The gate knows nothing about tools, the network, or the workflows that use it.
"""

from __future__ import annotations

from typing import Iterable

__all__ = ["ScopeRuleError", "ScopeGate", "normalize_target"]


class ScopeRuleError(ValueError):
    """Raised when a scope pattern is malformed (fail closed at construction)."""


_WILDCARD_PREFIX = "*."
_HTTP_MARKERS = ("://", "/", "?", "#", "@", ":")


def normalize_target(target: object) -> str:
    """Reduce a target URL/host to a bare, lowercase hostname.

    ``https://a.example.com:8443/x`` -> ``a.example.com``.
    """
    text = str(target if target is not None else "").strip().lower()
    if not text:
        return ""
    if "://" in text:
        text = text.split("://", 1)[1]
    if "@" in text:  # strip userinfo
        text = text.rsplit("@", 1)[1]
    # path / query / fragment
    for sep in ("/", "?", "#"):
        if sep in text:
            text = text.split(sep, 1)[0]
    if text.startswith("["):  # IPv6 literal, e.g. [::1]:443
        end = text.find("]")
        if end != -1:
            return text[1:end]
    if ":" in text:  # port
        text = text.split(":", 1)[0]
    return text


def _validate_pattern(pattern: object, field: str) -> str:
    if not isinstance(pattern, str) or not pattern.strip():
        raise ScopeRuleError(f"{field} rule must be a non-empty string")
    rule = pattern.strip()
    if rule != rule.lower():
        raise ScopeRuleError(f"{field} rule {pattern!r} must be lowercase")
    for marker in ("://", "/", "?", "#", "@", ":"):
        if marker in rule:
            raise ScopeRuleError(
                f"{field} rule {pattern!r} must be a bare host "
                "(no scheme, path, port, or credentials)"
            )
    if " " in rule or "\t" in rule:
        raise ScopeRuleError(f"{field} rule {pattern!r} must not contain whitespace")
    if rule.startswith(_WILDCARD_PREFIX):
        base = rule[len(_WILDCARD_PREFIX):]
        if not base or "*" in base:
            raise ScopeRuleError(
                f"{field} rule {pattern!r} must be '*.host' with a concrete host"
            )
    elif "*" in rule:
        raise ScopeRuleError(
            f"{field} rule {pattern!r} may only use a leading '*.' wildcard"
        )
    return rule


def _matches(rule: str, host: str) -> bool:
    if rule.startswith(_WILDCARD_PREFIX):
        base = rule[len(_WILDCARD_PREFIX):]
        return host == base or host.endswith("." + base)
    return host == rule


class ScopeGate:
    """Pure, deterministic allow/deny decision for a target hostname."""

    def __init__(
        self,
        in_scope: list[str],
        out_of_scope: list[str] | None = None,
    ) -> None:
        self.in_scope: tuple[str, ...] = tuple(
            _validate_pattern(rule, "in-scope") for rule in (in_scope or [])
        )
        self.out_of_scope: tuple[str, ...] = tuple(
            _validate_pattern(rule, "out-of-scope") for rule in (out_of_scope or [])
        )

    # ------------------------------------------------------------------ #
    # Decisions
    # ------------------------------------------------------------------ #

    def is_allowed(self, target: str) -> tuple[bool, str]:
        """Return ``(allowed, reason)``; ``reason`` is always one of four forms."""
        host = normalize_target(target)

        for rule in self.out_of_scope:
            if _matches(rule, host):
                return False, f"blocked: matched out-of-scope rule {rule}"

        if not self.in_scope:
            return False, "blocked: no in-scope rules defined"

        for rule in self.in_scope:
            if _matches(rule, host):
                return True, f"allowed: matched in-scope rule {rule}"

        return False, "blocked: target not in scope"

    def filter(self, targets: Iterable[str]) -> tuple[list[str], list[str]]:
        """Split ``targets`` into ``(allowed, blocked)`` preserving input order."""
        allowed: list[str] = []
        blocked: list[str] = []
        for target in targets:
            ok, _reason = self.is_allowed(target)
            (allowed if ok else blocked).append(target)
        return allowed, blocked

    def blocked_reasons(self, targets: Iterable[str]) -> list[dict[str, str]]:
        """Record ``{"target", "reason"}`` for each blocked target (audit trail)."""
        records: list[dict[str, str]] = []
        for target in targets:
            ok, reason = self.is_allowed(target)
            if not ok:
                records.append({"target": str(target), "reason": reason})
        return records
