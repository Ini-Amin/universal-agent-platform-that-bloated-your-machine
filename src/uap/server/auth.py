"""Opt-in bearer-token auth for the HTTP/WS API.

Local-first default: when ``UAP_API_TOKEN`` is unset auth is **disabled** and the
server behaves exactly as before (single user, bound to 127.0.0.1). Setting the
env var turns every non-exempt route into "token required" -- including
``POST /approvals/{id}/decide``, the route that makes the governance layer real.

Credential channels:

* HTTP -> ``Authorization: Bearer <token>``.
* WebSocket -> ``?token=<token>`` query parameter. Browsers cannot set headers
  on a WS handshake, and the alternative (a cookie) would make the API
  CSRF-reachable from any page; one documented channel is enough.

Exempt paths are the UI shell only (``/``, ``/index.html``, ``/js/*``,
``/css/*``, ``/legacy``) so the page can load and then present its token.

The token is compared with :func:`secrets.compare_digest` and is never logged
nor echoed into a response body.
"""

from __future__ import annotations

import logging
import os
import re
import secrets

from fastapi import HTTPException
from starlette.exceptions import WebSocketException
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocket

__all__ = [
    "api_token",
    "auth_enabled",
    "require_token",
    "is_exempt",
    "close_unauthorized_websocket",
    "scrub_token_from_logs",
    "EXEMPT_PREFIXES",
]

#: UI-shell paths reachable without a token. Prefix match on the request path.
#: The static mounts (``/``, ``/ui``, ``/legacy``) bypass route dependencies
#: anyway; these entries cover the FastAPI routes that shadow them.
EXEMPT_PREFIXES: tuple[str, ...] = ("/js/", "/css/", "/legacy")

#: Exact paths reachable without a token.
_EXEMPT_PATHS: frozenset[str] = frozenset({"/", "/index.html"})

_WS_TOKEN_PARAM = "token"


def api_token() -> str | None:
    """The configured token, or ``None`` when auth is disabled.

    Read on every call (not cached) so tests and ``os.environ`` edits take
    effect without rebuilding the app. An empty/whitespace value counts as
    unset -- a blank token must not look like security.
    """
    raw = os.environ.get("UAP_API_TOKEN")
    if raw is None:
        return None
    token = raw.strip()
    return token or None


def auth_enabled() -> bool:
    """``True`` when ``UAP_API_TOKEN`` is set to a non-empty value."""
    return api_token() is not None


def is_exempt(path: str) -> bool:
    """``True`` for UI-shell paths that stay reachable without a token."""
    return path in _EXEMPT_PATHS or path.startswith(EXEMPT_PREFIXES)


def _presented(conn: HTTPConnection) -> str | None:
    """Pull the candidate token off the connection, or ``None``."""
    header = conn.headers.get("authorization")
    if header is not None:
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
        return None
    if conn.scope.get("type") == "websocket":
        return conn.query_params.get(_WS_TOKEN_PARAM) or None
    return None


def _matches(expected: str, presented: str | None) -> bool:
    """Constant-time token comparison (``compare_digest`` on UTF-8 bytes)."""
    if presented is None:
        return False
    return secrets.compare_digest(expected.encode("utf-8"), presented.encode("utf-8"))


async def require_token(conn: HTTPConnection) -> None:
    """App-level dependency: fail closed unless the request carries the token.

    Declared with :class:`~starlette.requests.HTTPConnection` (not ``Request``)
    so FastAPI can resolve it for websocket routes too.

    Raises:
        HTTPException: 401 ``{"detail": "unauthorized"}`` for HTTP requests.
        WebSocketException: close code 1008 (policy violation) for handshakes.
    """
    expected = api_token()
    if expected is None or is_exempt(conn.url.path):
        return
    if _matches(expected, _presented(conn)):
        return
    if conn.scope.get("type") == "websocket":
        raise WebSocketException(code=1008, reason="unauthorized")
    raise HTTPException(status_code=401, detail="unauthorized")


async def close_unauthorized_websocket(
    websocket: WebSocket, exc: WebSocketException,
) -> None:
    """Exception handler that closes a rejected handshake with ``exc.code``.

    Starlette's default handler closes *before* accepting, which uvicorn turns
    into an HTTP 403 -- the browser then reports an opaque 1006 and the client
    cannot tell "unauthorized" from "server down". Accepting first makes the
    1008 close frame actually reach the client (verified against live uvicorn).
    """
    await websocket.accept()
    await websocket.close(code=exc.code, reason=exc.reason or "")


#: Matches the WS credential channel (``?token=`` / ``&token=``) in any string.
_TOKEN_IN_URL = re.compile(r"(?i)([?&]token=)[^&\s\"']+")


class _ScrubTokenFilter(logging.Filter):
    """Replaces ``token=<secret>`` with ``token=***`` in log records.

    Uvicorn's access log prints the full request line, so a WS handshake
    carrying ``?token=`` would otherwise write the secret to stdout.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                _TOKEN_IN_URL.sub(r"\1***", a) if isinstance(a, str) else a
                for a in record.args
            )
        if isinstance(record.msg, str):
            record.msg = _TOKEN_IN_URL.sub(r"\1***", record.msg)
        return True


#: Loggers that print request lines. ``uvicorn.error`` is not a typo: uvicorn
#: logs the WebSocket handshake line (which carries ``?token=``) there, not on
#: ``uvicorn.access`` (verified in uvicorn's websockets_sansio_impl).
_REQUEST_LOGGERS = ("uvicorn.access", "uvicorn.error")


def scrub_token_from_logs() -> None:
    """Install the scrubbing filter on the request loggers (idempotent)."""
    for name in _REQUEST_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, _ScrubTokenFilter) for f in logger.filters):
            logger.addFilter(_ScrubTokenFilter())
