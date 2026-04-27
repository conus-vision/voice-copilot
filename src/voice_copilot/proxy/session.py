"""Session registry — maps proxy clients to stable session IDs.

One "session" = one running CLI (Claude Code, Codex, aider, ...) talking to
our reverse-proxy. We key on `(user-agent, auth-prefix)` so:
  * the same CLI with the same credentials keeps a stable id across requests;
  * two different CLIs (or the same CLI launched twice with different keys)
    show up as two sessions.

The registry is in-memory, cheap, and shared with commentator + /api/sessions.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Pull a short, human-friendly label out of a User-Agent.
# Matches `claude-cli/1.2.3`, `codex/0.5`, `aider 0.74`, `python-httpx/0.27`.
_UA_LABEL = re.compile(r"([A-Za-z][A-Za-z0-9._-]{0,30})[/ ](\d[\d.]*)")


@dataclass
class Session:
    id: str
    label: str
    user_agent: str
    provider: str  # "anthropic" | "openai" | "gemini" | ...
    cli_id: str | None
    first_seen: float
    last_seen: float
    request_count: int = 0
    last_query: str | None = None  # latest user message sniffed from request body
    last_method: str | None = None
    last_path: str | None = None
    last_request_bytes: int | None = None

    def touch(self) -> None:
        self.last_seen = time.time()
        self.request_count += 1

    def observe_request(
        self,
        *,
        method: str,
        path: str,
        request_bytes: int,
        query: str | None = None,
    ) -> None:
        self.last_seen = time.time()
        self.last_method = method
        self.last_path = path
        self.last_request_bytes = request_bytes
        if query:
            self.last_query = query

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "user_agent": self.user_agent,
            "provider": self.provider,
            "cli_id": self.cli_id,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "request_count": self.request_count,
            "last_query": self.last_query,
            "last_method": self.last_method,
            "last_path": self.last_path,
            "last_request_bytes": self.last_request_bytes,
        }


class SessionRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._active_id: str | None = None
        self._lock = threading.Lock()
        self._listeners: list[Callable[[], None]] = []

    # ------------------------------------------------------------------ lookup / create

    def identify(
        self,
        headers: Mapping[str, str],
        *,
        provider: str,
    ) -> Session:
        """Return the (existing or new) Session for this request."""
        ua = headers.get("user-agent") or headers.get("User-Agent") or ""
        auth = (
            headers.get("authorization")
            or headers.get("Authorization")
            or headers.get("x-api-key")
            or headers.get("X-Api-Key")
            or ""
        )
        key_src = f"{provider}|{ua}|{auth[:16]}"
        sid = hashlib.sha1(key_src.encode("utf-8")).hexdigest()[:12]

        with self._lock:
            sess = self._sessions.get(sid)
            if sess is None:
                sess = Session(
                    id=sid,
                    label=_label_from_ua(ua, provider),
                    user_agent=ua,
                    provider=provider,
                    cli_id=_cli_id_from_ua(ua),
                    first_seen=time.time(),
                    last_seen=time.time(),
                )
                self._sessions[sid] = sess
                if self._active_id is None:
                    self._active_id = sid
                log.info("proxy: new session %s (%s, provider=%s)", sid, sess.label, provider)
                self._notify()
            sess.touch()
        return sess

    # ------------------------------------------------------------------ active

    def get_active_id(self) -> str | None:
        with self._lock:
            return self._active_id

    def set_active_id(self, sid: str | None) -> bool:
        with self._lock:
            if sid is not None and sid not in self._sessions:
                return False
            self._active_id = sid
        self._notify()
        return True

    def all(self) -> list[Session]:
        with self._lock:
            # Newest-last-seen first.
            return sorted(self._sessions.values(), key=lambda s: s.last_seen, reverse=True)

    def observe_request(
        self,
        sid: str,
        *,
        method: str,
        path: str,
        request_bytes: int,
        query: str | None = None,
    ) -> None:
        active_changed = False
        with self._lock:
            sess = self._sessions.get(sid)
            if sess is None:
                return
            sess.observe_request(
                method=method,
                path=path,
                request_bytes=request_bytes,
                query=query,
            )
            if query and self._active_id != sid:
                self._active_id = sid
                active_changed = True
        if active_changed:
            log.info("proxy: auto-selected active session %s from observed query", sid)
        self._notify()

    # ------------------------------------------------------------------ change notification

    def on_change(self, cb: Callable[[], None]) -> None:
        self._listeners.append(cb)

    def _notify(self) -> None:
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:
                log.exception("session registry listener failed")


def _label_from_ua(ua: str, provider: str) -> str:
    if not ua:
        return f"{provider}-client"
    m = _UA_LABEL.search(ua)
    if m:
        return m.group(1)
    return ua[:30]


def _cli_id_from_ua(ua: str) -> str | None:
    lowered = ua.lower()
    for cli_id in ("claude", "codex", "copilot", "aider", "opencode", "kimi"):
        if cli_id in lowered:
            return cli_id
    if "github cli" in lowered or "gh " in lowered:
        return "copilot"
    return None
