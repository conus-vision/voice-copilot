"""Session registry — maps proxy clients to stable session IDs.

One "session" = one running CLI (Claude Code, Codex, aider, ...) talking to
our reverse-proxy. Most CLIs put their own session id on every model request
(`X-Claude-Code-Session-Id`, Codex's `session-id`, OpenCode's `x-session-id`);
a request that carries one is keyed on it, so two copies of the same CLI with
the same credentials are still two sessions. The key is the one the companion
hub gives the same session when the CLI's plugin reports it, so proxy and
plugin describe one session under one id.

A request without such an id falls back to `(user-agent, auth-prefix)`: the
same CLI with the same credentials keeps one id across requests, two different
CLIs (or the same CLI with different keys) show up as two sessions.

The registry is in-memory, cheap, and shared with commentator + /api/sessions.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: A proxy session nothing was heard from for this long is dropped from the
#: list when a new one appears. Keyed by the CLI's session id, a CLI makes a
#: new one on every `/clear`; one that comes back gets its old id again.
IDLE_FORGET_S = 1800.0

#: Headers that carry the CLI's own session id, most specific first.
_SESSION_HEADERS = (
    "x-claude-code-session-id",  # Claude Code
    "session-id",  # Codex
    "session_id",  # older Codex; Pi with OpenAI-style providers
    "x-session-id",  # OpenCode, Crush; Pi with OpenRouter-style providers
    "x-session-affinity",  # OpenCode, Crush, Pi
    "x-task-id",  # Cline
    "x-opencode-session",  # OpenCode Zen
)

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
    #: The proxy has seen this session's model traffic.
    proxied: bool = False
    #: A plugin or hook reports this session; the companion hub ends it.
    external: bool = False

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
            "via": "+".join(
                name for name, on in (("proxy", self.proxied), ("plugin", self.external)) if on
            ),
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
        lowered = {name.lower(): value for name, value in headers.items()}
        ua = lowered.get("user-agent", "")
        cli_id = _cli_id_from_ua(ua)
        native_id = client_session_id(lowered)
        if native_id:
            sid = session_key(cli_id or provider, native_id)
        else:
            auth = lowered.get("authorization") or lowered.get("x-api-key") or ""
            key_src = f"{provider}|{ua}|{auth[:16]}"
            sid = hashlib.sha1(key_src.encode("utf-8")).hexdigest()[:12]

        created = False
        with self._lock:
            sess = self._sessions.get(sid)
            if sess is None:
                created = True
                self._forget_idle()
                sess = Session(
                    id=sid,
                    label=_label_from_ua(ua, provider),
                    user_agent=ua,
                    provider=provider,
                    cli_id=cli_id,
                    first_seen=time.time(),
                    last_seen=time.time(),
                )
                self._sessions[sid] = sess
                if self._active_id is None:
                    self._active_id = sid
                log.info("proxy: new session %s (%s, provider=%s)", sid, sess.label, provider)
            elif not sess.proxied:
                # A plugin reported this session first; its model traffic now
                # comes through here too.
                sess.user_agent = sess.user_agent or ua
                log.info("proxy: session %s is also proxied", sid)
            sess.proxied = True
            sess.touch()
        # Outside the lock: a listener that reads the registry back (all(),
        # get_active_id()) would otherwise deadlock on the non-reentrant lock.
        if created:
            self._notify()
        return sess

    def register_external(
        self,
        sid: str,
        *,
        label: str,
        cli_id: str | None,
        provider: str = "hooks",
    ) -> Session:
        """Return the session a plugin or hook reports under `sid`, creating it once.

        Proxy sessions are keyed by who is calling; a CLI that reports its own
        events through the companion API names its session itself.
        """
        created = False
        with self._lock:
            sess = self._sessions.get(sid)
            if sess is None:
                created = True
                now = time.time()
                sess = Session(
                    id=sid,
                    label=label,
                    user_agent="",
                    provider=provider,
                    cli_id=cli_id,
                    first_seen=now,
                    last_seen=now,
                )
                self._sessions[sid] = sess
                if self._active_id is None:
                    self._active_id = sid
                log.info("companion: new session %s (%s)", sid, label)
            sess.external = True
            sess.touch()
        if created:
            self._notify()
        return sess

    def observe_query(self, sid: str, query: str) -> None:
        """A user prompt arrived in `sid`: remember it and make the session active.

        Same rule as a proxied request that carries a question: whichever
        terminal the user just typed into is the one worth narrating.
        """
        with self._lock:
            sess = self._sessions.get(sid)
            if sess is None:
                return
            sess.last_seen = time.time()
            sess.last_query = query
            changed = self._active_id != sid
            self._active_id = sid
        if changed:
            log.info("companion: auto-selected active session %s from a user prompt", sid)
        self._notify()

    def remove(self, sid: str) -> None:
        """Forget a session that ended; the active one falls back to the newest left."""
        with self._lock:
            if self._sessions.pop(sid, None) is None:
                return
            if self._active_id == sid:
                newest = max(self._sessions.values(), key=lambda s: s.last_seen, default=None)
                self._active_id = newest.id if newest else None
        self._notify()

    def proxied(self, sid: str) -> bool:
        """Whether the proxy carries this session's model traffic."""
        with self._lock:
            sess = self._sessions.get(sid)
            return sess is not None and sess.proxied

    def _forget_idle(self) -> None:
        # Caller holds the lock. Plugin sessions end through the companion hub.
        cutoff = time.time() - IDLE_FORGET_S
        for sid in [
            sid
            for sid, sess in self._sessions.items()
            if sess.last_seen < cutoff and not sess.external and sid != self._active_id
        ]:
            del self._sessions[sid]
            log.info("proxy: forgetting idle session %s", sid)

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
            # Every request of a turn repeats the question. Only a new one
            # means the user just typed there; otherwise two agents working
            # at once would take the narration from each other on every call.
            asked = bool(query) and query != sess.last_query
            sess.observe_request(
                method=method,
                path=path,
                request_bytes=request_bytes,
                query=query,
            )
            if asked and self._active_id != sid:
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


def session_key(cli: str, native_id: str) -> str:
    """The registry id of a CLI session known by the CLI's own id.

    Hashed, not truncated: time-ordered ids (UUIDv7, Pi's) share their first
    digits across sessions started within the same minute.
    """
    return f"{cli}-{hashlib.sha1(native_id.encode('utf-8')).hexdigest()[:8]}"


def client_session_id(headers: Mapping[str, str]) -> str | None:
    """The session id a CLI sends with every model request, if it sends one.

    `headers` has lower-case names. Codex's turn metadata names the session
    even on a sub-agent's requests, whose `session-id` may differ.
    """
    raw = headers.get("x-codex-turn-metadata")
    if raw:
        try:
            meta = json.loads(raw)
        except ValueError:
            meta = None
        if isinstance(meta, dict):
            value = meta.get("session_id")
            if isinstance(value, str) and value.strip():
                return value.strip()[:200]
    for name in _SESSION_HEADERS:
        value = headers.get(name, "").strip()
        if value:
            return value[:200]
    return None


def _label_from_ua(ua: str, provider: str) -> str:
    if not ua:
        return f"{provider}-client"
    m = _UA_LABEL.search(ua)
    if m:
        return m.group(1)
    return ua[:30]


def _cli_id_from_ua(ua: str) -> str | None:
    lowered = ua.lower()
    for cli_id in ("claude", "codex", "copilot", "aider", "opencode", "kimi", "qwen", "gemini"):
        if cli_id in lowered:
            return cli_id
    if "github cli" in lowered or "gh " in lowered:
        return "copilot"
    return None
