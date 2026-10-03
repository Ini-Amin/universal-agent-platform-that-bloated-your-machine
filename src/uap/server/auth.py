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

import hashlib
from dataclasses import dataclass

import logging
import os
import re
import secrets

from fastapi import FastAPI, HTTPException
from starlette.exceptions import WebSocketException
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket

__all__ = [
    "EXEMPT_PREFIXES",
    "Identity",
    "api_token",
    "auth_enabled",
    "close_unauthorized_websocket",
    "get_current_identity",
    "guard_fastapi_builtin_routes",
    "is_exempt",
    "require_admin",
    "require_role",
    "require_token",
    "require_writer",
    "resolve_identity",
    "scrub_token_from_logs",
]


@dataclass(frozen=True)
class Identity:
    """The authenticated security principal for a request."""

    user_id: str | None
    name: str
    role: str  # "admin" | "member" | "viewer"
    is_bootstrap: bool = False

#: UI-shell paths reachable without a token. Prefix match on the request path.
#: The static mounts (``/``, ``/ui``, ``/legacy``) bypass route dependencies
#: anyway; these entries cover the FastAPI routes that shadow them.
EXEMPT_PREFIXES: tuple[str, ...] = ("/js/", "/css/", "/legacy")

#: Exact paths reachable without a token.
_EXEMPT_PATHS: frozenset[str] = frozenset({"/", "/index.html"})

_WS_TOKEN_PARAM = "token"

#: Set once :func:`guard_fastapi_builtin_routes` has patched ``FastAPI.setup``.
_BUILTIN_ROUTES_GUARDED = False


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
    """``True`` when ``UAP_API_TOKEN`` is set or registered users exist."""
    if api_token() is not None:
        return True
    try:
        from uap.db.engine import session_scope
        from uap.db.repositories import UserRepository

        with session_scope() as session:
            return UserRepository(session).has_users()
    except Exception:
        return False


def resolve_identity(conn: HTTPConnection) -> Identity | None:
    """Resolve the authenticated Identity from the connection, or ``None``."""
    presented = _presented(conn)
    if presented is None:
        return None

    # 1. Bootstrap operator token (UAP_API_TOKEN acts as admin credential)
    expected = api_token()
    if expected is not None and _matches(expected, presented):
        return Identity(
            user_id="operator",
            name="Operator Admin",
            role="admin",
            is_bootstrap=True,
        )

    # 2. Hashed user API key lookup
    key_hash = hashlib.sha256(presented.encode("utf-8")).hexdigest()
    try:
        from uap.db.engine import session_scope
        from uap.db.repositories import UserRepository

        with session_scope() as session:
            repo = UserRepository(session)
            user = repo.get_by_api_key_hash(key_hash)
            if user is not None and user.is_active:
                repo.touch_last_seen(user.id)
                session.commit()
                return Identity(
                    user_id=user.id,
                    name=user.name,
                    role=user.role,
                    is_bootstrap=False,
                )
    except Exception:
        pass

    return None

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
    """App-level dependency: fail closed unless the request carries a valid credential."""
    identity = resolve_identity(conn)
    if identity is not None:
        conn.state.identity = identity
        conn.state.user_id = identity.user_id
        conn.state.user_role = identity.role
        return

    if is_exempt(conn.url.path):
        return

    if not auth_enabled():
        return

    if conn.scope.get("type") == "websocket":
        raise WebSocketException(code=1008, reason="unauthorized")
    raise HTTPException(status_code=401, detail="unauthorized")


def get_current_identity(conn: HTTPConnection) -> Identity | None:
    """Retrieve the resolved identity from connection state, or None."""
    return getattr(conn.state, "identity", None)


def require_role(*allowed_roles: str):
    """Dependency enforcing that the caller has one of the specified roles."""

    async def dependency(conn: HTTPConnection) -> Identity | None:
        identity = get_current_identity(conn)
        if identity is None:
            if not auth_enabled():
                return None
            raise HTTPException(status_code=401, detail="unauthorized")
        if identity.role not in allowed_roles:
            raise HTTPException(
                status_code=403, detail="forbidden: insufficient permissions"
            )
        return identity

    return dependency


require_admin = require_role("admin")
require_writer = require_role("admin", "member")

def _token_gated(app: ASGIApp) -> ASGIApp:
    """Wrap an ASGI app with the very check :func:`require_token` runs.

    Reuses :func:`_presented` / :func:`_matches` / :func:`is_exempt`, so the
    module keeps exactly one definition of "does this request carry the
    token", and answers with the same 401 body.
    """

    async def gated(scope: Scope, receive: Receive, send: Send) -> None:
        if not auth_enabled() or scope["type"] != "http" or is_exempt(scope["path"]):
            await app(scope, receive, send)
            return
        req = Request(scope, receive)
        identity = resolve_identity(req)
        if identity is not None:
            if "state" not in scope:
                scope["state"] = {}
            scope["state"]["identity"] = identity
            await app(scope, receive, send)
            return
        denial = JSONResponse({"detail": "unauthorized"}, status_code=401)
        await denial(scope, receive, send)
    return gated


def guard_fastapi_builtin_routes() -> None:
    """Put the token gate on ``/openapi.json``, ``/docs`` and ``/redoc``.

    Those three -- plus ``/docs/oauth2-redirect`` -- are registered by
    ``FastAPI.setup`` as plain Starlette routes, so the app-level
    ``Depends(require_token)`` never runs for them: with ``UAP_API_TOKEN``
    set, ``GET /openapi.json`` still handed every route and request schema to
    an anonymous caller. They were never exempt, only *unreachable* by the
    dependency, so the gate is applied where they are registered instead.

    ``ponytail:`` this patches ``FastAPI.setup``, a third-party class, for the
    whole process; only apps that declared :func:`require_token` as a router
    dependency are wrapped, so other FastAPI apps here are untouched. If the
    app factory ever grows a middleware slot, move this to
    ``app.add_middleware(...)`` in ``create_app`` and delete this function.
    """
    global _BUILTIN_ROUTES_GUARDED
    if _BUILTIN_ROUTES_GUARDED:
        return
    original_setup = FastAPI.setup

    def setup(self: FastAPI) -> None:
        known = {id(route) for route in self.router.routes}
        original_setup(self)
        opted_in = any(
            getattr(dep, "dependency", None) is require_token
            for dep in getattr(self.router, "dependencies", ())
        )
        if opted_in:
            for route in self.router.routes:
                if id(route) not in known:  # a built-in this call just added
                    route.app = _token_gated(route.app)

    FastAPI.setup = setup
    _BUILTIN_ROUTES_GUARDED = True


guard_fastapi_builtin_routes()


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
