"""Refuse requests to the local panel that a web page could forge.

The panel listens on loopback, but loopback is reachable from every page open
in the user's browser, and the panel's API launches terminals, writes PATH
shims into the shell profile, stores API keys and streams everything the
agent does:

* A cross-site HTML form (or ``fetch`` in ``no-cors`` mode) sends a POST
  without a CORS preflight, and several endpoints act on a bare POST.
* Browsers apply no same-origin policy to WebSockets, so any page could open
  ``/ws``, read the live trace and send commands.
* DNS rebinding points a hostile domain at 127.0.0.1, which makes its pages
  same-origin with themselves; only the ``Host`` header still names it.

So a request must carry a ``Host`` the panel answers to (a loopback name, an
IP literal, or the name the server was bound to), and a state-changing
request or WebSocket handshake that carries an ``Origin`` must come from that
same host and port. Clients without an ``Origin`` (curl, scripts) pass:
browsers always send one on cross-origin POSTs and on WebSocket handshakes.

A request from another device must also show the panel's token (see
`access.py`); a valid ``?token=`` on a page load is swapped for a cookie.
"""

from __future__ import annotations

import ipaddress
import logging
from urllib.parse import urlsplit

from starlette.datastructures import URL
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from voice_copilot.web.access import PanelAccess

log = logging.getLogger(__name__)

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_LOOPBACK_NAMES = frozenset({"localhost"})
#: Bind addresses that name no host in particular; they add nothing to the allowlist.
_WILDCARD_HOSTS = frozenset({"", "0.0.0.0", "::"})
#: WebSocket close code for "policy violation".
_WS_POLICY_VIOLATION = 1008

_NEEDS_LINK_PAGE = """<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Voice Copilot</title>
<body style="font: 16px/1.5 system-ui, sans-serif; max-width: 34rem; margin: 3rem auto;
padding: 0 16px; color: #1c2230; background: #f6f7fb">
<h1 style="font-size: 1.3rem">This panel needs its access link</h1>
<p>Voice Copilot answers other devices only with its token. Open the link that
<code>voice-copilot</code> printed when it started, the one that ends in
<code>?token=</code>. After that, this browser remembers it.</p>
</body>"""


def _hostname(netloc: str) -> str | None:
    """`host[:port]` → lowercased host, without brackets or a trailing dot."""
    try:
        name = urlsplit(f"//{netloc.strip()}").hostname
    except ValueError:
        return None
    return name.rstrip(".") if name else None


def _is_ip_literal(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


class LocalOriginGuard:
    """ASGI middleware for the panel app; see the module docstring."""

    def __init__(
        self, app: ASGIApp, *, bind_host: str | None = None, access: PanelAccess | None = None
    ) -> None:
        self.app = app
        bound = _hostname(bind_host or "") or ""
        self._extra_hosts = frozenset() if bound in _WILDCARD_HOSTS else frozenset({bound})
        self._access = access if access is not None else PanelAccess()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers") or []
        }
        reason = self._refusal(scope, headers.get("host", ""), headers.get("origin"))
        if reason is not None:
            log.warning("refused %s %s: %s", scope["type"], scope.get("path", ""), reason)
            if scope["type"] == "websocket":
                await WebSocketClose(code=_WS_POLICY_VIOLATION)(scope, receive, send)
                return
            await PlainTextResponse(f"voice-copilot: {reason}", status_code=403)(
                scope, receive, send
            )
            return
        client = scope.get("client")
        if not self._access.required(client[0] if client else None):
            await self.app(scope, receive, send)
            return
        if self._access.accepts(self._access.presented(headers)):
            await self.app(scope, receive, send)
            return
        link = self._access.from_link(scope.get("query_string", b"").decode("latin-1"))
        if self._access.accepts(link):
            if scope["type"] == "http" and "text/html" in headers.get("accept", ""):
                # Trade the link for a cookie, and keep the token out of the
                # address bar and the history.
                url = URL(scope=scope).remove_query_params("token")
                target = url.path + (f"?{url.query}" if url.query else "")
                response = RedirectResponse(target, status_code=303)
                response.headers["set-cookie"] = self._access.cookie()
                await response(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return
        log.warning(
            "refused %s %s from %s: no panel token",
            scope["type"],
            scope.get("path", ""),
            client[0] if client else "?",
        )
        if scope["type"] == "websocket":
            await WebSocketClose(code=_WS_POLICY_VIOLATION)(scope, receive, send)
        elif "text/html" in headers.get("accept", ""):
            await HTMLResponse(_NEEDS_LINK_PAGE, status_code=401)(scope, receive, send)
        else:
            await PlainTextResponse("voice-copilot: panel token required", status_code=401)(
                scope, receive, send
            )

    def _host_allowed(self, name: str | None) -> bool:
        if not name:
            return False
        return name in _LOOPBACK_NAMES or name in self._extra_hosts or _is_ip_literal(name)

    def _refusal(self, scope: Scope, host: str, origin: str | None) -> str | None:
        """Why this request must be refused, or None to let it through."""
        if not self._host_allowed(_hostname(host)):
            return f"unexpected Host header {host!r}"
        if scope["type"] == "http" and scope.get("method", "GET") in _SAFE_METHODS:
            return None
        if origin is None:
            return None
        if origin.strip().lower() == "null":
            return "opaque Origin"
        try:
            origin_netloc = urlsplit(origin.strip()).netloc.lower()
        except ValueError:
            return f"malformed Origin {origin!r}"
        if origin_netloc != host.strip().lower():
            return f"cross-origin request from {origin!r}"
        return None
