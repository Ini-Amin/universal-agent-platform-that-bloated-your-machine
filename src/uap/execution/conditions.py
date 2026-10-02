"""Deterministic, safe condition evaluator for CONDITION nodes (section 18).

No ``eval``/``exec`` is used anywhere: the grammar is tokenised and parsed by
hand, so an untrusted expression string can never execute code. The evaluator is
total: :func:`evaluate` never raises and always returns a ``(result, reason)``
tuple.

Grammar
-------
::

    expression := "true" | "false"
                | <path> <op> <literal>

    <path>     := identifier ( "." identifier )*
    <op>       := "==" | "!=" | ">" | ">=" | "<" | "<=" | "in" | "not in"
    <literal>  := <string> | <number> | "true" | "false" | "[" <literal> ("," <literal>)* "]"
    <string>   := "'" ... "'" | '"' ... '"'
    <number>   := "-"? digit+ ( "." digit+ )?

``<path>`` is resolved against a scope dict shaped like::

    {
        "inputs": { ... the run inputs ... },
        "node":   { "<node_id>": { "<port>": <value>, ... }, ... },
    }

Dotted access walks nested dicts (e.g. ``node.plan.confidence``). A path that
does not resolve yields ``(False, "missing path: <path>")``.

Return contract
---------------
* valid expression -> ``(bool, reason)`` where ``reason`` is human readable.
* missing path      -> ``(False, "missing path: <path>")``.
* unknown operator  -> ``(False, "unknown operator: <op>")``.
* any other problem -> ``(False, "invalid expression: <detail>")``.
"""

from __future__ import annotations

from typing import Any, NamedTuple

__all__ = ["evaluate"]


class _ParseError(Exception):
    """Internal: raised while tokenising/parsing; never escapes :func:`evaluate`."""


class _Token(NamedTuple):
    kind: str  # "path" | "op" | "in" | "not_in" | "string" | "number" | "bool" | "list"
    value: Any
    raw: str


_KNOWN_TWO_CHAR_OPS = ("==", "!=", ">=", "<=")
_KNOWN_ONE_CHAR_OPS = ("<", ">")
_PUNCTUATION = set("=!~^%&|+-*/@$?")


# --------------------------------------------------------------------------- #
# Tokenizer
# --------------------------------------------------------------------------- #


def _tokenize(expression: str) -> list[_Token]:
    tokens: list[_Token] = []
    i = 0
    n = len(expression)
    while i < n:
        char = expression[i]
        if char.isspace():
            i += 1
            continue

        two = expression[i : i + 2]
        if two in _KNOWN_TWO_CHAR_OPS:
            tokens.append(_Token("op", two, two))
            i += 2
            continue
        if char in _KNOWN_ONE_CHAR_OPS:
            tokens.append(_Token("op", char, char))
            i += 1
            continue
        if char in ("'", '"'):
            end = expression.find(char, i + 1)
            if end == -1:
                raise _ParseError(f"unterminated string starting at position {i}")
            tokens.append(_Token("string", expression[i + 1 : end], expression[i : end + 1]))
            i = end + 1
            continue

        if char == "[":
            end = expression.find("]", i + 1)
            if end == -1:
                raise _ParseError("unterminated list literal")
            raw = expression[i : end + 1]
            tokens.append(_Token("list", _parse_list(raw), raw))
            i = end + 1
            continue

        if char.isdigit() or (char == "-" and i + 1 < n and expression[i + 1].isdigit()):
            j = i + 1
            while j < n and (expression[j].isdigit() or expression[j] == "."):
                j += 1
            raw = expression[i:j]
            try:
                value: Any = float(raw) if "." in raw else int(raw)
            except ValueError as exc:  # pragma: no cover - defensive
                raise _ParseError(f"invalid number {raw!r}") from exc
            tokens.append(_Token("number", value, raw))
            i = j
            continue

        if char.isalpha() or char == "_":
            j = i
            while j < n and (expression[j].isalnum() or expression[j] in "._"):
                j += 1
            raw = expression[i:j]
            lowered = raw.lower()
            if lowered == "true":
                tokens.append(_Token("bool", True, raw))
            elif lowered == "false":
                tokens.append(_Token("bool", False, raw))
            elif lowered == "in":
                tokens.append(_Token("in", "in", raw))
            elif lowered == "not":
                k = j
                while k < n and expression[k].isspace():
                    k += 1
                m = k
                while m < n and expression[m].isalpha():
                    m += 1
                if expression[k:m].lower() == "in":
                    tokens.append(_Token("not_in", "not in", expression[i:m]))
                    j = m
                else:
                    tokens.append(_Token("path", raw, raw))
            else:
                tokens.append(_Token("path", raw, raw))
            i = j
            continue

        if char in _PUNCTUATION:
            run = char
            j = i + 1
            while j < n and expression[j] in _PUNCTUATION:
                run += expression[j]
                j += 1
            raise _ParseError(f"unknown operator: {run}")

        raise _ParseError(f"unexpected character {char!r} at position {i}")
    return tokens

# --------------------------------------------------------------------------- #
# Literals
# --------------------------------------------------------------------------- #


def _parse_list(raw: str) -> list[Any]:
    inner = raw[1:-1].strip()
    if not inner:
        return []
    return [_parse_literal(part) for part in _split_top_level(inner)]


def _split_top_level(text: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    quote: str | None = None
    for char in text:
        if quote is not None:
            current.append(char)
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
            current.append(char)
        elif char == "[":
            depth += 1
            current.append(char)
        elif char == "]":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    if current:
        parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def _parse_literal(text: str) -> Any:
    token = text.strip()
    if not token:
        raise _ParseError("empty literal")
    if len(token) >= 2 and token[0] in ("'", '"') and token[-1] == token[0]:
        return token[1:-1]
    lowered = token.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if token.startswith("["):
        if not token.endswith("]"):
            raise _ParseError(f"malformed list {token!r}")
        return _parse_list(token)
    try:
        return float(token) if "." in token else int(token)
    except ValueError as exc:
        raise _ParseError(f"invalid literal {token!r}") from exc


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #


def _resolve_path(path: str, scope: dict[str, Any]) -> tuple[bool, Any]:
    current: Any = scope
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return False, None
    return True, current


def _compare(operator: str, left: Any, right: Any) -> bool:
    if operator == "==":
        return bool(left == right)
    if operator == "!=":
        return bool(left != right)
    if operator == ">":
        return bool(left > right)
    if operator == ">=":
        return bool(left >= right)
    if operator == "<":
        return bool(left < right)
    if operator == "<=":
        return bool(left <= right)
    if operator == "in":
        return bool(left in right)
    if operator == "not in":
        return bool(left not in right)
    raise _ParseError(f"unknown operator: {operator}")


def _eval_tokens(tokens: list[_Token], scope: dict[str, Any]) -> tuple[bool, str]:
    if not tokens:
        raise _ParseError("empty expression")

    if len(tokens) == 1:
        only = tokens[0]
        if only.kind == "bool":
            return bool(only.value), f"literal {only.raw}"
        raise _ParseError("expected '<path> <op> <literal>'")

    if len(tokens) != 3:
        raise _ParseError("expected exactly '<path> <op> <literal>'")

    path_token, op_token, literal_token = tokens
    if path_token.kind != "path":
        raise _ParseError("left-hand side must be a path")
    if op_token.kind not in ("op", "in", "not_in"):
        raise _ParseError(f"unknown operator: {op_token.raw}")
    if literal_token.kind not in ("string", "number", "bool", "list"):
        raise _ParseError("right-hand side must be a literal (quoted string, number, true/false or list)")

    found, value = _resolve_path(path_token.value, scope)
    if not found:
        return False, f"missing path: {path_token.value}"

    try:
        result = _compare(op_token.value, value, literal_token.value)
    except TypeError:
        return (
            False,
            f"type mismatch: cannot compare {type(value).__name__} with "
            f"{type(literal_token.value).__name__}",
        )
    return result, f"{path_token.value} {op_token.raw} {literal_token.raw} -> {result}"


def evaluate(expression: str, scope: dict[str, Any]) -> tuple[bool, str]:
    """Evaluate ``expression`` against ``scope``; never raises.

    Returns ``(result, reason)``. See the module docstring for the grammar and
    the exact reason strings.
    """
    try:
        tokens = _tokenize(expression if isinstance(expression, str) else str(expression))
    except _ParseError as exc:
        return False, f"invalid expression: {exc}"
    except Exception as exc:  # noqa: BLE001 - total function contract
        return False, f"invalid expression: {type(exc).__name__}: {exc}"

    try:
        return _eval_tokens(tokens, scope)
    except _ParseError as exc:
        detail = str(exc)
        if detail.startswith("unknown operator"):
            return False, detail
        return False, f"invalid expression: {detail}"
    except Exception as exc:  # noqa: BLE001 - total function contract
        return False, f"invalid expression: {type(exc).__name__}: {exc}"
