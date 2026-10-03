"""Robust response parsing for the 9router gateway.

Why this exists: a whole session's worth of benchmarking was WRONG because the
responses were parsed with a bare ``json.loads()``. 9router returns THREE
different shapes depending on the provider and whether streaming is negotiated:

1. a plain JSON object;
2. concatenated JSON objects with no separator (SSE frames stripped of their
   newlines), which makes ``json.loads`` raise ``Extra data: line 1 column N``;
3. real SSE lines (``data: {...}`` followed by ``data: [DONE]``).

The failure was silent in the worst way: a parse error was reported as "the
model returned empty content", so three models were wrongly judged to be
broken. ``gpt-6-astra`` was measured at 119s when it actually answers in 8s;
``claude-opus-4-8`` and ``claude-opus-5`` were called "empty" when they were
answering correctly the whole time.

Use :func:`extract` for every 9router call. Never ``json.loads`` the raw body.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["parse_any", "extract"]


def parse_any(raw: str) -> dict[str, Any] | None:
    """Parse a 9router body in any of its three shapes, or return ``None``."""

    if not raw:
        return None

    # Shape 1: a plain JSON object.
    try:
        return json.loads(raw)
    except Exception:
        pass

    # Shape 2: concatenated JSON. Take the first complete object.
    try:
        obj, _ = json.JSONDecoder().raw_decode(raw.lstrip())
        return obj
    except Exception:
        pass

    # Shape 3: SSE. Merge every delta into one message.
    merged: dict[str, Any] = {}
    usage: dict[str, Any] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload in ("", "[DONE]"):
            continue
        try:
            frame = json.loads(payload)
        except Exception:
            continue
        choices = frame.get("choices") or [{}]
        choice = choices[0] if choices else {}
        for key in ("delta", "message"):
            piece = (choice.get(key) or {}).get("content")
            if piece:
                merged["content"] = merged.get("content", "") + piece
        if frame.get("usage"):
            usage = frame["usage"]
        if frame.get("model"):
            merged["model"] = frame["model"]
    if merged or usage:
        merged["usage"] = usage
        return merged
    return None


def extract(raw: str) -> tuple[str, int, int, str, str]:
    """Return ``(content, reasoning_tokens, completion_tokens, model, finish)``.

    Never raises: an unparseable body yields empty strings and zeros, so the
    caller can distinguish "the model said nothing" from "we failed to read it"
    only if it also checks :func:`parse_any`.
    """

    parsed = parse_any(raw)
    if not parsed:
        return "", 0, 0, "", ""

    usage = parsed.get("usage") or {}
    reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
    completion = usage.get("completion_tokens", 0)

    content = (parsed.get("content") or "").strip()
    finish = ""
    if not content:
        choices = parsed.get("choices") or [{}]
        choice = choices[0] if choices else {}
        content = ((choice.get("message") or {}).get("content") or "").strip()
        if not content:
            content = ((choice.get("delta") or {}).get("content") or "").strip()
        finish = choice.get("finish_reason") or ""

    return content, int(reasoning or 0), int(completion or 0), parsed.get("model", ""), finish
