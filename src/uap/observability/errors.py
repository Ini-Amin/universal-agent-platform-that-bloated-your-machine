"""Error observability and redaction helpers.

Ensures no exception is silently swallowed and sensitive tokens/secrets
are scrubbed before logging.
"""

from __future__ import annotations

import logging
import re
from typing import Any

__all__ = ["log_swallowed_exception", "redact_text"]

_REDACTION_PATTERNS = [
    re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9_\-\.~+/]+=*)"),
    re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,})\b"),
    re.compile(r"\b(ghp_[A-Za-z0-9]{20,})\b"),
    re.compile(r"\b(AKIA[0-9A-Z]{16})\b"),
    re.compile(r"(?i)\b((?:api[_-]?key|token|secret|password|auth|credential)\s*[:=]\s*[\"']?)([^\s\"',;]+)"),
    re.compile(r"\b(eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})\b"),
    re.compile(r"://([^:]+):([^@]+)@"),
]


def redact_text(text: Any) -> str:
    """Scrub token-looking strings and credential patterns from text."""
    s = str(text) if text is not None else ""
    s = _REDACTION_PATTERNS[0].sub(r"\g<1>[REDACTED]", s)
    s = _REDACTION_PATTERNS[1].sub("[REDACTED]", s)
    s = _REDACTION_PATTERNS[2].sub("[REDACTED]", s)
    s = _REDACTION_PATTERNS[3].sub("[REDACTED]", s)
    s = _REDACTION_PATTERNS[4].sub(r"\g<1>[REDACTED]", s)
    s = _REDACTION_PATTERNS[5].sub("[REDACTED]", s)
    s = _REDACTION_PATTERNS[6].sub(r"://\1:[REDACTED]@", s)
    return s


def log_swallowed_exception(
    logger: logging.Logger,
    exc: BaseException | None,
    message: str,
    *,
    level: int = logging.WARNING,
    **context: Any,
) -> str:
    """Log an handled/swallowed exception with sanitized text and context.

    Returns the formatted diagnostic string.
    """
    clean_msg = redact_text(message)
    exc_part = f"{type(exc).__name__}: {redact_text(str(exc))}" if exc is not None else ""
    ctx_parts = [f"{k}={redact_text(v)}" for k, v in sorted(context.items())]
    ctx_str = f" [{', '.join(ctx_parts)}]" if ctx_parts else ""
    
    if exc_part:
        formatted = f"{clean_msg} ({exc_part}){ctx_str}"
    else:
        formatted = f"{clean_msg}{ctx_str}"

    logger.log(level, "%s", formatted)
    return formatted
