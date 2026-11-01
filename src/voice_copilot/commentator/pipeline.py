"""Commentator pipeline: debounce events, stream an LLM, emit utterances.

Flow per session:

    bus → classify/filter → debounce buffer
        → narration LLM (streamed to TTS)
        → summary-update LLM (internal memory)

Each session keeps two pieces of state:

  * `user_query` — what the user actually asked the agent (sniffed from the
    request body by the proxy, or `None` when we don't know it).
  * `summary`   — a 2-3 sentence rolling memo of what the agent has done so
    far AND what we've already narrated. Updated by a short non-streaming
    LLM call after every narration, and fed back into the next narration
    call so the model doesn't lose the thread or repeat itself.

A high-importance event (error, file edit, agent awaiting input) forces an
immediate flush so we don't leave the user in silence when something urgent
just happened.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, cast
from uuid import uuid4

from voice_copilot.commentator.cli_profiles import NARRATION_PROFILES, strong_model_for
from voice_copilot.commentator.format import (
    build_ask_user,
    build_narration_user,
    build_summary_user,
    build_supervisor_user,
    edit_excerpt,
    format_events,
    plain_output,
    read_edited_files,
    speech_text,
)
from voice_copilot.commentator.importance import classify, meets_threshold
from voice_copilot.commentator.prompts import load as load_prompt
from voice_copilot.commentator.prompts import load_ask as load_ask_prompt
from voice_copilot.commentator.prompts import load_summary as load_summary_prompt
from voice_copilot.commentator.prompts import load_supervisor as load_supervisor_prompt
from voice_copilot.core.bus import EventBus
from voice_copilot.core.config import CommentatorConfig, Language
from voice_copilot.core.events import Event, EventKind
from voice_copilot.core.user_query import clean_user_query
from voice_copilot.providers import registry
from voice_copilot.providers.llm.base import LLMMessage, LLMProvider
from voice_copilot.proxy.session import SessionRegistry

log = logging.getLogger(__name__)

_NO_SESSION_KEY = "_none"

#: How long a narrator or supervisor call may take. Without a limit a model that never
#: answers means silence with nothing in the panel to say why. Long enough for a local
#: model that has to load first.
_LLM_TIMEOUT_S = 120.0
_FAILURE_KEY_CHARS = 120
_FAILURE_LABEL_CHARS = 200


#: Injected blocks the CLI wraps around the human's turn. Stripped before the
#: text is used as the narration anchor — they change between requests, and a
#: changed anchor throws away everything we had queued up to say.
@dataclass
class _SessionContext:
    user_query: str | None = None
    #: What the user told the agent on top of `user_query` while it worked.
    corrections: list[str] = field(default_factory=list)
    summary: str | None = None
    # Monotonic "query version" — bumped each time a new USER_QUERY lands.
    # Events that arrived before the latest query are dropped from narration
    # so we don't describe a previous question's tail.
    query_version: int = 0
    playback_rate: float = 1.2
    playback_ready_for_next: bool = False
    last_narrated_query_version: int = -1
    # Supervisor bookkeeping: a rolling transcript of formatted events (the
    # narrator only ever sees the last batch), tool calls since the last
    # review, and how often each failure came up — a repeat is a checkpoint.
    history: list[str] = field(default_factory=list)
    tools_since_review: int = 0
    last_verdict: tuple[str, str] | None = None
    failures: dict[str, int] = field(default_factory=dict)
    #: A failure that just came up again, for the next review: (description, count).
    repeated_failure: tuple[str, int] | None = None
    #: The turn ended while a review was still running.
    final_review_due: _Batch | None = None
    #: For the supervisor's sense of progress on the current request.
    started_at: float = field(default_factory=time.monotonic)
    tool_calls: int = 0
    files_edited: set[str] = field(default_factory=set)
    #: The latest changes the agent made, for the supervisor to read.
    edits: list[str] = field(default_factory=list)
    #: The session's folder, and the files it changed with the latest last.
    cwd: str | None = None
    recent_files: list[str] = field(default_factory=list)
    #: Events on record already: `vc ask` may get to them before the narrator does.
    recorded: set[str] = field(default_factory=set)
    last_event_at: float | None = None

    @property
    def quiet_s(self) -> int | None:
        """How long the agent has said nothing, as far as we can tell."""
        return None if self.last_event_at is None else int(time.monotonic() - self.last_event_at)

    @property
    def progress(self) -> str:
        minutes = (time.monotonic() - self.started_at) / 60
        spent = f"{minutes:.0f} min" if minutes >= 1 else "under a minute"
        return (
            f"{spent} since the request, {self.tool_calls} tool calls, "
            f"{len(self.files_edited)} files edited"
        )

    @property
    def goal(self) -> str | None:
        """The task as it stands now, for the narrator's and the supervisor's prompts."""
        if not self.user_query or not self.corrections:
            return self.user_query
        added = "; ".join(self.corrections)
        return f"{self.user_query}\nThen, while the agent worked, the user added: {added}"


@dataclass
class _Batch:
    """Events + the snapshot of session state they belong to."""

    events: list[Event] = field(default_factory=list)
    session_key: str = _NO_SESSION_KEY
    user_query: str | None = None
    summary: str | None = None
    query_version: int = 0
    #: Nothing has been narrated for this question yet — the line should open
    #: with what the task is before describing the work.
    opening: bool = False


class Commentator:
    def __init__(
        self,
        bus: EventBus,
        cfg: CommentatorConfig,
        language: Language,
        llm: LLMProvider | None = None,
        sessions: SessionRegistry | None = None,
    ) -> None:
        self._bus = bus
        self._cfg = cfg
        self._language = language
        self._llm = llm or _build_llm(cfg)
        self._prompt_style = getattr(self._llm, "prompt_style", "api")
        self._system_prompt = load_prompt(language, self._prompt_style)
        self._summary_prompt = load_summary_prompt(language, self._prompt_style)
        self._sessions = sessions

        self._llm_provider_key = _provider_key(cfg)
        self._supervisor_llm: LLMProvider | None = None
        self._supervisor_key: str | None = None
        self._supervisor_prompt = load_supervisor_prompt(language, self._prompt_style)
        self._supervisor_tasks: set[asyncio.Task[None]] = set()

        self._buffer: list[Event] = []
        self._max_buffer = 60
        # When the current buffer started filling — drives the idle timer that
        # speaks up during long stretches of quiet tool work.
        self._buffer_since: float | None = None
        self._speak_task: asyncio.Task[None] | None = None
        # Hold refs to in-flight summary tasks so the GC doesn't cancel them
        # mid-flight; they self-discard from this set on completion.
        self._summary_tasks: set[asyncio.Task[None]] = set()

        # Per-session context; key is session_id or `_NO_SESSION_KEY` for
        # events without one (CLI adapters, internal sources).
        self._contexts: dict[str, _SessionContext] = {}
        #: For `vc log` and `vc cost`.
        self._said: deque[str] = deque(maxlen=_SAID_LINES)
        self._usage: Counter[str] = Counter()

    @property
    def supervisor_mode(self) -> str:
        return self._cfg.supervisor.mode

    def models(self) -> dict[str, str]:
        """What the narrator and the supervisor use, by the name `vc model` gives it."""
        provider = self._cfg.provider
        model = provider.options.get("model")
        if model and provider.name == "openai-compat":
            narrator = f"ollama/{model}"
        else:
            cli = str(provider.options.get("cli") or provider.name)
            profile = NARRATION_PROFILES.get(cli)
            narrator = str(model or (profile.model if profile else None) or f"{cli} default")
        strong = strong_model_for(str(provider.options.get("cli") or ""))
        supervisor = self._cfg.supervisor.model or strong or narrator
        return {"narrator": narrator, "supervisor": str(supervisor)}

    def recent_lines(self, count: int) -> list[str]:
        return list(self._said)[-count:]

    def usage(self) -> Counter[str]:
        return Counter(self._usage)

    def _count(self, role: str, sent: str, got: str) -> None:
        self._usage[f"{role}.calls"] += 1
        self._usage[f"{role}.in"] += len(sent)
        self._usage[f"{role}.out"] += len(got)

    def _files_for_models(self, ctx: _SessionContext) -> tuple[list[str], list[str]]:
        """(edits, whole files) that `vc access` lets the models read."""
        access = self._cfg.file_access
        if access == "events":
            return [], []
        if access == "edits":
            return ctx.edits, []
        latest = list(dict.fromkeys(reversed(ctx.recent_files)))[:_WHOLE_FILES]
        return ctx.edits, read_edited_files(latest, ctx.cwd)

    async def say_line(self, text: str, *, session_key: str | None = None) -> None:
        """Speak this as it is, with no model involved."""
        ctx = self._contexts.setdefault(session_key or _NO_SESSION_KEY, _SessionContext())
        await self._emit(
            {
                "utterance_id": f"say-{uuid4().hex}",
                "text": text,
                "read_text": speech_text(text),
                "streaming": False,
                "language": self._language,
                "role": "answer",
                "query_version": ctx.query_version,
                **({"session_id": session_key} if session_key else {}),
            }
        )

    async def answer_question(self, question: str, session_id: str | None) -> str:
        """`vc ask`: the second model answers the user, out loud. The agent is not involved."""
        key = session_id or _NO_SESSION_KEY
        ctx = self._contexts.setdefault(key, _SessionContext())
        # What the narrator hasn't got to yet happened too, and may be the very thing asked about.
        self._record_events(key, [ev for ev in self._buffer if self._session_key_of(ev) == key])
        edits, files = self._files_for_models(ctx)
        user_content = build_ask_user(
            question=question,
            user_query=ctx.goal,
            summary=ctx.summary,
            history=ctx.history,
            progress=ctx.progress,
            edits=edits,
            files=files,
        )
        system = load_ask_prompt(self._language, self._prompt_style)
        pieces: list[str] = []
        async with asyncio.timeout(_LLM_TIMEOUT_S):
            stream = self._supervisor().stream_chat(
                [LLMMessage(role="user", content=user_content)],
                system=system,
                max_tokens=300,
                temperature=0.2,
            )
            async for delta in stream:
                if delta:
                    pieces.append(delta)
        answer = "".join(pieces).strip()
        if not answer:
            raise RuntimeError("the model returned no text")
        self._count("question", system + user_content, answer)
        await self.say_line(answer, session_key=session_id)
        return answer

    def update_config(self, cfg: CommentatorConfig, language: Language | None = None) -> None:
        """Hot-reload provider / settings without restarting. Called when user saves config."""
        key = _provider_key(cfg)
        if key != self._llm_provider_key:
            log.info(
                "commentator: LLM provider changed (%s → %s), rebuilding",
                self._llm_provider_key,
                key,
            )
            self._llm = _build_llm(cfg)
            self._llm_provider_key = key
            self._prompt_style = getattr(self._llm, "prompt_style", "api")
            lang = language if language is not None else self._language
            self._system_prompt = load_prompt(lang, self._prompt_style)
            self._summary_prompt = load_summary_prompt(lang, self._prompt_style)
        self._cfg = cfg
        if language is not None and language != self._language:
            self._language = language
            self._system_prompt = load_prompt(language, self._prompt_style)
            self._summary_prompt = load_summary_prompt(language, self._prompt_style)
            self._supervisor_prompt = load_supervisor_prompt(language, self._prompt_style)
        # The supervisor LLM is rebuilt lazily at its next checkpoint if the
        # provider or model changed — `_supervisor()` compares keys.

    async def run(self) -> None:
        """Main loop — subscribe to the bus and narrate until cancelled."""
        debounce_s = max(0.05, self._cfg.debounce_ms / 1000.0)
        # Maximum time to keep accumulating events before forcing a flush,
        # even when new events arrive continuously (e.g. long thinking streams).
        # 4x debounce is a sensible default: responsive but not spammy.
        max_batch_s = debounce_s * 4

        loop = asyncio.get_event_loop()

        async with self._bus.subscribe() as q:
            while True:
                try:
                    event = await self._next_event(q)
                except TimeoutError:
                    # Only quiet activity for a while. Say something rather
                    # than leaving the user listening to nothing.
                    if not await self._flush(trigger="idle"):
                        self._buffer_since = time.monotonic()
                    continue
                control = self._consume_control_event(event)
                if control == "flush":
                    await self._flush(trigger="playback_ready")
                    continue
                if control == "check":
                    await self._check_now(event)
                    continue
                if control == "consume":
                    continue  # USER_MESSAGE etc. — context-only, not narrated
                admitted = self._admit(event)
                if admitted is None or admitted == "context":
                    continue
                if admitted == "high":
                    await self._flush(trigger="high")
                    continue

                # Accumulate until the bus goes quiet for debounce_s OR until
                # max_batch_s has elapsed — whichever comes first. The deadline
                # prevents indefinitely deferring narration during long thinking
                # streams where events arrive faster than the debounce window.
                deadline = loop.time() + max_batch_s
                try:
                    while True:
                        remaining = deadline - loop.time()
                        if remaining <= 0:
                            break  # max batch time reached → flush now
                        event = await asyncio.wait_for(
                            q.get(),
                            timeout=min(debounce_s, remaining),
                        )
                        control = self._consume_control_event(event)
                        if control == "flush":
                            await self._flush(trigger="playback_ready")
                            continue
                        if control == "check":
                            await self._check_now(event)
                            continue
                        if control == "consume":
                            continue
                        if self._admit(event) == "high":
                            break
                except TimeoutError:
                    pass

                await self._flush(trigger="normal")

    async def _next_event(self, q: asyncio.Queue[Event]) -> Event:
        """Next bus event, or raise TimeoutError when the idle window expires."""
        timeout = self._idle_timeout()
        if timeout is None:
            return await q.get()
        return await asyncio.wait_for(q.get(), timeout=timeout)

    def _idle_timeout(self) -> float | None:
        idle_s = self._cfg.idle_narration_ms / 1000.0
        if idle_s <= 0 or not self._buffer or self._buffer_since is None:
            return None
        return max(0.05, self._buffer_since + idle_s - time.monotonic())

    # ----------------------------------------------------------------- context

    def _consume_control_event(self, event: Event) -> str | None:
        """Intercept events that update session context but don't narrate.

        USER_MESSAGE addressed to the agent (sniffed from a proxy request
        body, or forwarded by a CLI adapter) is the anchor for subsequent
        narration. USER_MESSAGE from our own mic/STT/hotkey plumbing is the
        user talking to *us*, not to the agent, and the dialog manager
        handles it — we ignore it here.
        """
        if event.kind is EventKind.USER_CHECK:
            return "check"

        if event.kind is EventKind.NARRATION_STOP:
            only = event.payload.get("session_id")
            self._buffer = (
                [ev for ev in self._buffer if self._session_key_of(ev) != only] if only else []
            )
            if not self._buffer:
                self._buffer_since = None
            for key, ctx in self._contexts.items():
                if only in (None, key):
                    ctx.query_version += 1  # whatever is being generated now comes back stale
                    ctx.final_review_due = None
            return "consume"

        if event.kind is EventKind.SESSION_STARTED:
            # Not consumed: only noted, for what `vc access files` may read.
            cwd = event.payload.get("cwd")
            if isinstance(cwd, str):
                self._contexts.setdefault(self._session_key_of(event), _SessionContext()).cwd = cwd
            return None

        if event.kind is EventKind.USER_MESSAGE:
            if event.source.startswith(("stt.", "web.", "hotkey")):
                return None
            text = event.payload.get("text") if isinstance(event.payload, dict) else None
            if not isinstance(text, str) or not text.strip():
                return "consume"
            cleaned = clean_user_query(text)
            if cleaned is None:
                # A title-generation call, a quota probe or a safety-classifier
                # pass — narrating against those (and dropping the buffer for
                # them) is what made narration lose the thread mid-turn.
                log.debug("narrate: ignoring non-user request text: %s", _truncate_log(text))
                return "consume"
            text = cleaned
            key = self._session_key_of(event)
            ctx = self._contexts.setdefault(key, _SessionContext())
            if ctx.user_query and _is_correction(event):
                # Said to a working agent: it changes the task, it doesn't replace it.
                # As the new "goal", "skip the README" would make the rest of the work
                # look unrelated, and the supervisor would stop the agent for it.
                if text != ctx.user_query and text not in ctx.corrections:
                    ctx.corrections.append(text)
                    ctx.history.append(f"- the user said: {text}")
                return "consume"
            if ctx.user_query != text:
                dropped = sum(1 for ev in self._buffer if self._session_key_of(ev) == key)
                if dropped:
                    self._buffer = [ev for ev in self._buffer if self._session_key_of(ev) != key]
                ctx.user_query = text
                ctx.corrections = []
                ctx.failures = {}
                ctx.repeated_failure = None
                ctx.started_at = time.monotonic()
                ctx.tool_calls = 0
                ctx.files_edited = set()
                ctx.edits = []
                ctx.summary = None  # fresh question → forget previous thread
                ctx.query_version += 1
                ctx.playback_ready_for_next = False
                log.info(
                    "narrate: new USER_QUERY for session=%s (v%d, dropped=%d buffered events): %s",
                    key,
                    ctx.query_version,
                    dropped,
                    _truncate_log(text),
                )
            return "consume"

        if event.kind in (EventKind.PLAYBACK_STATE, EventKind.PLAYBACK_READY):
            key = self._session_key_of(event)
            ctx = self._contexts.setdefault(key, _SessionContext())
            if isinstance(event.payload, dict):
                rate = event.payload.get("playback_rate")
                if isinstance(rate, (int, float)) and rate > 0:
                    ctx.playback_rate = float(rate)
            if event.kind is EventKind.PLAYBACK_READY:
                ctx.playback_ready_for_next = True
                if any(self._session_key_of(ev) == key for ev in self._buffer):
                    return "flush"
            return "consume"

        return None

    def _session_key_of(self, event: Event) -> str:
        if isinstance(event.payload, dict):
            sid = event.payload.get("session_id")
            if isinstance(sid, str) and sid:
                return sid
        return _NO_SESSION_KEY

    def _admit(self, event: Event) -> str | None:
        """Buffer the event and report what it warrants.

        Returns None (dropped), "context", or the importance that should drive
        the flush. Sub-threshold events — a Read, a Glob, a turn ending — are
        still buffered: they are how the narrator knows which files and tools
        are in play. They just never wake it on their own; that stays the job
        of thinking, answers, edits and failures.
        """
        imp = classify(event, self._cfg)
        if imp is None:
            return None
        if event.source.startswith("commentator"):
            return None  # don't narrate our own utterances
        if not self._accepts(event):
            return None  # filtered by active-session rule
        self._contexts.setdefault(
            self._session_key_of(event), _SessionContext()
        ).last_event_at = time.monotonic()
        if not self._buffer:
            self._buffer_since = time.monotonic()
        self._buffer.append(event)
        if len(self._buffer) > self._max_buffer:
            self._buffer = self._buffer[-self._max_buffer :]
        if not meets_threshold(imp, self._cfg.min_importance):
            return "context"
        return imp

    def _accepts(self, event: Event) -> bool:
        """Drop events from non-active proxy sessions."""
        if self._sessions is None:
            return True
        sid = event.payload.get("session_id") if isinstance(event.payload, dict) else None
        if not sid:
            return True
        active = self._sessions.get_active_id()
        return active is None or active == sid

    # ------------------------------------------------------------------ flush

    async def _flush(self, trigger: str = "normal") -> bool:
        """Kick off a narration for the buffered events. True if one started."""
        if not self._buffer:
            return False
        if self._speak_task is not None and not self._speak_task.done():
            return False

        events = list(self._buffer)
        # All events in a batch are anchored to the session of the *last*
        # event — in practice they're from the same session because the
        # filter already rejects cross-session mixing.
        session_key = self._session_key_of(events[-1])
        ctx = self._contexts.setdefault(session_key, _SessionContext())
        batch = _Batch(
            events=events,
            session_key=session_key,
            user_query=ctx.goal,
            summary=ctx.summary,
            query_version=ctx.query_version,
            opening=(trigger == "idle" and ctx.query_version > ctx.last_narrated_query_version),
        )
        word_count = self._batch_word_count(batch.events)
        if not self._should_flush(batch, ctx, trigger=trigger, word_count=word_count):
            return False
        self._buffer = []
        self._buffer_since = None
        # Now, not after the narration: a question may come while that model is thinking.
        self._record_events(session_key, events)
        log.info(
            "narrate: kickoff batch of %d events (session=%s, trigger=%s, words=%d, has_query=%s, has_summary=%s, rate=%.2f)",
            len(events),
            session_key,
            trigger,
            word_count,
            bool(ctx.user_query),
            bool(ctx.summary),
            ctx.playback_rate,
        )
        self._speak_task = asyncio.create_task(
            self._drain_and_narrate(batch),
            name="commentator.narrate",
        )
        return True

    async def _drain_and_narrate(self, batch: _Batch) -> None:
        """Narrate one batch, update summary, then re-flush if more piled up."""
        given_up = False
        try:
            await self._narrate(batch)
            # Whatever became of the narration: a narrator that fails or stays silent
            # must not take the supervisor down with it.
            self._review_if_due(batch)
        except asyncio.CancelledError:
            # The user asked for a check and this call was in the way. Its events still count.
            given_up = True
            self._buffer = [*batch.events, *self._buffer]
            raise
        finally:
            if self._speak_task is asyncio.current_task():
                self._speak_task = None
            if self._buffer and not given_up:
                await self._flush(trigger="normal")

    # --------------------------------------------------------------- check

    async def _check_now(self, event: Event) -> None:
        """The user asked what is going on, so answer even if nothing new happened.

        Everything else here moves only when an event arrives. An agent stuck inside a
        tool sends none, so no line and no checkpoint ever comes, and a hung model call
        holds up the lines behind it. This is the way out of both.
        """
        key = self._check_target(event)
        ctx = self._contexts.setdefault(key, _SessionContext())
        await self._give_up_on_calls(key)
        recent = [line.lstrip("- ") for line in ctx.history[-_CHECK_RECENT_LINES:]]
        recent += [_format_for_recent(ev) for ev in self._buffer[-_CHECK_RECENT_LINES:]]
        payload: dict[str, Any] = {"quiet_s": ctx.quiet_s, "recent": recent[-_CHECK_RECENT_LINES:]}
        if key != _NO_SESSION_KEY:
            payload["session_id"] = key
        log.info("check: requested for session=%s, quiet for %s s", key, ctx.quiet_s)
        self._buffer.append(Event(kind=EventKind.USER_CHECK, source=event.source, payload=payload))
        self._buffer_since = None
        await self._flush(trigger="check")

    def _check_target(self, event: Event) -> str:
        sid = event.payload.get("session_id") or (
            self._sessions.get_active_id() if self._sessions is not None else None
        )
        if sid:
            return str(sid)
        return next(reversed(self._contexts), _NO_SESSION_KEY)

    async def _give_up_on_calls(self, key: str) -> None:
        running = [
            t
            for t in (self._speak_task, *self._supervisor_tasks)
            if t is not None
            and not t.done()
            and (t is self._speak_task or t.get_name() == f"commentator.supervisor:{key}")
        ]
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)

    # ------------------------------------------------------------ narration

    async def _narrate(self, batch: _Batch) -> None:
        user_content = build_narration_user(
            user_query=batch.user_query,
            summary=batch.summary,
            events=batch.events,
            style=self._prompt_style,
            opening=batch.opening,
        )
        messages = [LLMMessage(role="user", content=user_content)]
        pieces: list[str] = []
        utterance_id = batch.events[-1].id

        log.info(
            "narrate: starting (events=%d, provider=%s, model=%s)",
            len(batch.events),
            self._cfg.provider.name,
            self._cfg.provider.options.get("model", "<default>"),
        )
        log.info(
            "narrate: PROMPT\n--- system ---\n%s\n--- user ---\n%s\n--- end ---",
            self._system_prompt,
            user_content,
        )
        await self._bus.publish(
            Event(
                kind=EventKind.COMMENTATOR_PROMPT,
                source="commentator",
                payload={
                    "system": self._system_prompt,
                    "user": user_content,
                    "provider": self._cfg.provider.name,
                    "model": str(self._cfg.provider.options.get("model", "")),
                    "event_count": len(batch.events),
                    "query_version": batch.query_version,
                    **(
                        {"session_id": batch.session_key}
                        if batch.session_key != _NO_SESSION_KEY
                        else {}
                    ),
                },
            )
        )
        got_first = False

        session_payload = (
            {"session_id": batch.session_key} if batch.session_key != _NO_SESSION_KEY else {}
        )

        async def report(message: str) -> None:
            # Tagged with the batch's session so the panel files it in the same
            # Trace bucket as the prompt it failed on — untagged, it lands in the
            # no-session bucket and the user just sees a prompt with no reply.
            await self._bus.publish(
                Event(
                    kind=EventKind.ERROR,
                    source="commentator",
                    payload={"where": "llm", "message": message, **session_payload},
                )
            )

        try:
            async with asyncio.timeout(_LLM_TIMEOUT_S):
                stream = self._llm.stream_chat(
                    messages,
                    system=self._system_prompt,
                    max_tokens=160,
                    temperature=0.4,
                )
                async for delta in stream:
                    if not delta:
                        continue
                    if not got_first:
                        log.info("narrate: first delta (%d chars)", len(delta))
                        got_first = True
                    pieces.append(delta)
                    await self._emit(
                        {
                            "utterance_id": utterance_id,
                            "text": delta,
                            "streaming": True,
                            "language": self._language,
                            "query_version": batch.query_version,
                            **session_payload,
                        }
                    )
        except asyncio.CancelledError:
            log.info("narrate: cancelled after %d chars", sum(len(p) for p in pieces))
            raise
        except TimeoutError:
            log.warning("narrate: no answer in %.0f s", _LLM_TIMEOUT_S)
            await report(f"The narrator's model didn't answer in {_LLM_TIMEOUT_S:.0f} s.")
            return
        except Exception as e:
            log.exception("commentator LLM error")
            await report(str(e))
            return

        full = "".join(pieces).strip()
        if not full:
            log.warning("narrate: stream ended empty — model returned no tokens")
            await report("The narrator's model returned no text.")
            return
        if self._is_stale_batch(batch):
            log.info(
                "narrate: dropping stale batch for session=%s query_version=%d",
                batch.session_key,
                batch.query_version,
            )
            return
        log.info("narrate: done (%d chars)", len(full))
        self._count("narrator", self._system_prompt + user_content, full)
        await self._emit(
            {
                "utterance_id": utterance_id,
                "text": full,
                "read_text": speech_text(full),
                "streaming": False,
                "language": self._language,
                "query_version": batch.query_version,
                **session_payload,
            }
        )
        ctx = self._contexts.setdefault(batch.session_key, _SessionContext())
        ctx.last_narrated_query_version = batch.query_version
        ctx.playback_ready_for_next = False

        # Fire-and-forget summary update so the next batch starts immediately
        # if it arrives mid-update. Worst case the next narration sees a
        # slightly-stale summary — better than blocking TTS on a second LLM
        # round-trip.
        task = asyncio.create_task(
            self._update_summary(batch, full),
            name="commentator.summary",
        )
        self._summary_tasks.add(task)
        task.add_done_callback(self._summary_tasks.discard)

    # ---------------------------------------------------------- supervisor

    def _review_if_due(self, batch: _Batch) -> None:
        if self._is_stale_batch(batch):
            return
        ctx = self._contexts.setdefault(batch.session_key, _SessionContext())
        self._record_for_supervisor(batch)
        reason = self._checkpoint_reason(batch, ctx)
        if reason is not None:
            review = asyncio.create_task(
                self._review(batch, reason, ctx),
                name=f"commentator.supervisor:{batch.session_key}",
            )
            self._supervisor_tasks.add(review)
            review.add_done_callback(self._supervisor_tasks.discard)

    async def _review(self, batch: _Batch, reason: str, ctx: _SessionContext) -> None:
        await self._supervise(batch, reason)
        due, ctx.final_review_due = ctx.final_review_due, None
        if due is not None and due.query_version == ctx.query_version:
            await self._supervise(due, "turn ended")

    def _record_for_supervisor(self, batch: _Batch) -> None:
        self._record_events(batch.session_key, batch.events)

    def _record_events(self, session_key: str, events: list[Event]) -> None:
        """Put these on record, once each, as soon as anything may ask about them."""
        ctx = self._contexts.setdefault(session_key, _SessionContext())
        fresh = [
            ev for ev in events if ev.kind is not EventKind.USER_CHECK and ev.id not in ctx.recorded
        ]
        ctx.recorded.update(ev.id for ev in fresh)
        ctx.history.extend(format_events(fresh).splitlines())
        if len(ctx.history) > _SUPERVISOR_HISTORY_LINES:
            ctx.history = ctx.history[-_SUPERVISOR_HISTORY_LINES:]
        for ev in fresh:
            if ev.kind is EventKind.TOOL_CALL_STARTED:
                ctx.tools_since_review += 1
                ctx.tool_calls += 1
                edit = edit_excerpt(ev.payload)
                if edit:
                    ctx.edits = [*ctx.edits, edit][-_SUPERVISOR_EDITS:]
            elif ev.kind is EventKind.FILE_EDITED:
                ctx.files_edited.add(str(ev.payload.get("path")))
                ctx.recent_files.append(str(ev.payload.get("path")))
            elif ev.kind is EventKind.TOOL_CALL_FINISHED and ev.payload.get("is_error"):
                key, description = _describe_failure(ev.payload)
                count = ctx.failures[key] = ctx.failures.get(key, 0) + 1
                if count >= 2:
                    ctx.repeated_failure = (description, count)

    def _checkpoint_reason(self, batch: _Batch, ctx: _SessionContext) -> str | None:
        """Why the supervisor should look now, or None to stay quiet.

        Cheap model every few seconds, expensive model only here: the end of
        the agent's turn, a run of tool calls, or the same failure coming back.
        """
        sup = self._cfg.supervisor
        if sup.mode == "off":
            return None
        if batch.events[-1].kind is EventKind.USER_CHECK:
            return (
                f"{_CHECK_REASON}, {batch.events[-1].payload.get('quiet_s')} s since the last event"
            )
        turn_over = any(
            ev.kind is EventKind.TURN_ENDED
            and ev.payload.get("final") is not False
            and not ev.payload.get("subagent")
            for ev in batch.events
        )
        if self._review_in_flight(batch.session_key):
            # One review at a time per session; the next checkpoint will look. The end
            # of the turn has no next checkpoint, so that one waits its turn instead.
            if turn_over:
                ctx.final_review_due = batch
            return None
        if turn_over:
            return "turn ended"
        if ctx.repeated_failure is not None:
            description, count = ctx.repeated_failure
            ctx.repeated_failure = None  # reviewed once per recurrence, not at every batch
            return f"the same failure came up {count} times: {description}"
        if sup.every_n_tools > 0 and ctx.tools_since_review >= sup.every_n_tools:
            return f"{ctx.tools_since_review} tool calls since the last review"
        return None

    def _review_in_flight(self, session_key: str) -> bool:
        return any(
            not t.done() and t.get_name() == f"commentator.supervisor:{session_key}"
            for t in self._supervisor_tasks
        )

    def _supervisor(self) -> LLMProvider:
        """The supervisor's LLM: the narrator's provider with a stronger model."""
        sup = self._cfg.supervisor
        provider = self._cfg.provider
        options = dict(provider.options)
        model = sup.model or (
            strong_model_for(str(options.get("cli") or "")) if provider.name == "auto" else None
        )
        if model:
            options["model"] = model
        key = f"{provider.name}:{sorted(options.items())}"
        if self._supervisor_llm is None or key != self._supervisor_key:
            self._supervisor_llm = cast(LLMProvider, registry.build("llm", provider.name, options))
            self._supervisor_key = key
            log.info(
                "supervisor: using %s model=%s", provider.name, options.get("model", "<default>")
            )
        return self._supervisor_llm

    async def _supervise(self, batch: _Batch, reason: str) -> None:
        ctx = self._contexts.setdefault(batch.session_key, _SessionContext())
        ctx.tools_since_review = 0
        edits, files = self._files_for_models(ctx)
        user_content = build_supervisor_user(
            user_query=batch.user_query,
            summary=ctx.summary or batch.summary,
            history=ctx.history,
            reason=reason,
            style=self._prompt_style,
            already_said=ctx.last_verdict[1] if ctx.last_verdict else None,
            progress=ctx.progress,
            edits=edits,
            files=files,
        )
        session_payload = (
            {"session_id": batch.session_key} if batch.session_key != _NO_SESSION_KEY else {}
        )
        log.info("supervisor: review (%s) for session=%s", reason, batch.session_key)
        pieces: list[str] = []
        try:
            llm = self._supervisor()
            async with asyncio.timeout(_LLM_TIMEOUT_S):
                stream = llm.stream_chat(
                    [LLMMessage(role="user", content=user_content)],
                    system=self._supervisor_prompt,
                    max_tokens=200,
                    temperature=0.2,
                )
                async for delta in stream:
                    if delta:
                        pieces.append(delta)
        except TimeoutError:
            log.warning("supervisor: no answer in %.0f s", _LLM_TIMEOUT_S)
            await self._bus.publish(
                Event(
                    kind=EventKind.ERROR,
                    source="commentator.supervisor",
                    payload={
                        "where": "supervisor",
                        "message": f"The supervisor's model didn't answer in {_LLM_TIMEOUT_S:.0f} s.",
                        **session_payload,
                    },
                )
            )
            return
        except RuntimeError as e:
            if _is_shutdown_error(e):
                return  # the run is over; nothing to review or report
            log.exception("supervisor LLM error")
            await self._bus.publish(
                Event(
                    kind=EventKind.ERROR,
                    source="commentator.supervisor",
                    payload={"where": "supervisor", "message": str(e), **session_payload},
                )
            )
            return
        except Exception as e:
            log.exception("supervisor LLM error")
            await self._bus.publish(
                Event(
                    kind=EventKind.ERROR,
                    source="commentator.supervisor",
                    payload={"where": "supervisor", "message": str(e), **session_payload},
                )
            )
            return
        reply = "".join(pieces)
        if not reply.strip():
            # Unreadable counts as OK below; no reply at all means nobody looked.
            await self._bus.publish(
                Event(
                    kind=EventKind.ERROR,
                    source="commentator.supervisor",
                    payload={
                        "where": "supervisor",
                        "message": "The supervisor's model returned no text, "
                        "so this checkpoint went unreviewed.",
                        **session_payload,
                    },
                )
            )
            return
        self._count("supervisor", self._supervisor_prompt + user_content, reply)
        status, message = parse_supervisor_verdict(reply)
        if ctx.query_version != batch.query_version:
            log.info("supervisor: dropping stale verdict for session=%s", batch.session_key)
            return
        mode = self._cfg.supervisor.mode
        acted = status == "stop" and mode == "guard"
        # The same complaint at every checkpoint is nagging, not oversight:
        # a verdict that restates the last spoken one goes to the Trace only.
        repeat = (
            status != "ok"
            and not reason.startswith(_CHECK_REASON)  # They asked: "as I said" is still an answer.
            and _same_verdict(ctx.last_verdict, (status, message))
        )
        if status != "ok":
            ctx.last_verdict = (status, message)
        log.info(
            "supervisor: %s%s — %s", status.upper(), " (pausing agent)" if acted else "", message
        )
        await self._bus.publish(
            Event(
                kind=EventKind.SUPERVISOR_VERDICT,
                source="commentator.supervisor",
                payload={
                    "status": status,
                    "message": message,
                    "reason": reason,
                    "paused_agent": acted,
                    "repeat": repeat,
                    "query_version": batch.query_version,
                    **session_payload,
                },
            )
        )
        if status == "ok" or not message or (repeat and not acted):
            return
        if acted:
            await self._bus.publish(
                Event(
                    kind=EventKind.SUPERVISOR_STOP,
                    source="commentator.supervisor",
                    payload={"message": message, **session_payload},
                )
            )
        spoken = _SPOKEN_PREFIX.get(self._language, _SPOKEN_PREFIX["en"])
        await self._emit(
            {
                "utterance_id": f"sup-{batch.events[-1].id}",
                "text": f"{spoken['stop'] if acted else spoken['warn']} {message}",
                "read_text": speech_text(
                    f"{spoken['stop'] if acted else spoken['warn']} {message}"
                ),
                "streaming": False,
                "language": self._language,
                "role": "supervisor",
                "query_version": batch.query_version,
                **session_payload,
            }
        )

    # ------------------------------------------------------------- summary

    async def _update_summary(self, batch: _Batch, narration: str) -> None:
        """Roll the running summary forward: prev + new events + what we said → new summary."""
        user_content = build_summary_user(
            prev_summary=batch.summary,
            events=batch.events,
            narration=narration,
            style=self._prompt_style,
        )
        messages = [LLMMessage(role="user", content=user_content)]
        pieces: list[str] = []
        try:
            stream = self._llm.stream_chat(
                messages,
                system=self._summary_prompt,
                max_tokens=220,
                temperature=0.2,
            )
            async for delta in stream:
                if delta:
                    pieces.append(delta)
        except RuntimeError as e:
            if not _is_shutdown_error(e):
                log.exception("commentator summary LLM error")
            return
        except Exception:
            log.exception("commentator summary LLM error")
            return

        new_summary = "".join(pieces).strip()
        self._count("summary", self._summary_prompt + user_content, new_summary)
        if not new_summary:
            log.info("summary: empty response, keeping previous")
            return

        ctx = self._contexts.setdefault(batch.session_key, _SessionContext())
        if ctx.query_version != batch.query_version:
            log.info(
                "summary: dropping stale update for session=%s (got v%d, latest v%d)",
                batch.session_key,
                batch.query_version,
                ctx.query_version,
            )
            return
        ctx.summary = new_summary
        log.info(
            "summary: updated for session=%s (%d chars): %s",
            batch.session_key,
            len(new_summary),
            _truncate_log(new_summary),
        )

    async def _emit(self, payload: dict[str, Any]) -> None:
        if not payload.get("streaming"):
            self._said.append(str(payload.get("text")))
        await self._bus.publish(
            Event(kind=EventKind.COMMENTATOR_UTTERANCE, source="commentator", payload=payload)
        )

    def _should_flush(
        self,
        batch: _Batch,
        ctx: _SessionContext,
        *,
        trigger: str,
        word_count: int,
    ) -> bool:
        if trigger in ("high", "check"):
            return True
        if trigger == "idle":
            # The whole point of the timer: the batch never reaches the usual
            # thinking/answer gate, so speak as long as anything happened.
            return self._has_something_to_say(batch.events)
        has_answer = self._has_answer_signal(batch.events)
        if not has_answer and not self._has_agent_signal(batch.events):
            return False
        if has_answer:
            return True
        if self._has_final_turn(batch.events):
            return True
        min_words, max_words = self._batch_word_bounds(ctx.playback_rate)
        first_for_query = batch.query_version > ctx.last_narrated_query_version
        if first_for_query:
            return word_count >= min_words
        if word_count >= max_words:
            return True
        if ctx.playback_ready_for_next:
            return word_count >= min_words
        return False

    def _has_something_to_say(self, events: list[Event]) -> bool:
        """Anything worth a sentence — a turn boundary on its own is not."""
        return any(
            ev.kind
            in (
                EventKind.AGENT_THINKING,
                EventKind.AGENT_TEXT,
                EventKind.TOOL_CALL_STARTED,
                EventKind.TOOL_CALL_FINISHED,
                EventKind.FILE_EDITED,
                EventKind.ERROR,
            )
            for ev in events
        )

    def _has_agent_signal(self, events: list[Event]) -> bool:
        return any(ev.kind in (EventKind.AGENT_THINKING, EventKind.AGENT_TEXT) for ev in events)

    def _has_answer_signal(self, events: list[Event]) -> bool:
        return any(ev.kind is EventKind.AGENT_TEXT for ev in events)

    def _has_final_turn(self, events: list[Event]) -> bool:
        has_answer = self._has_answer_signal(events)
        has_turn_end = any(ev.kind is EventKind.TURN_ENDED for ev in events)
        return has_answer and has_turn_end

    def _batch_word_bounds(self, playback_rate: float) -> tuple[int, int]:
        rate = min(max(playback_rate, 1.0), 2.5)
        min_words = max(16, min(36, round(16 * rate)))
        max_words = max(min_words + 20, min_words * 3)
        return min_words, max_words

    def _batch_word_count(self, events: list[Event]) -> int:
        total = 0
        for event in events:
            if not isinstance(event.payload, dict):
                continue
            for key in ("text", "delta", "thinking", "content", "summary", "tool_name", "path"):
                value = event.payload.get(key)
                if isinstance(value, str) and value.strip():
                    total += len(value.strip().split())
        return total

    def _is_stale_batch(self, batch: _Batch) -> bool:
        ctx = self._contexts.get(batch.session_key)
        return ctx is not None and ctx.query_version > batch.query_version


def _build_llm(cfg: CommentatorConfig) -> LLMProvider:
    return cast(LLMProvider, registry.build("llm", cfg.provider.name, dict(cfg.provider.options)))


#: How much of the transcript the supervisor gets to see.
_SUPERVISOR_HISTORY_LINES = 80
_SUPERVISOR_EDITS = 6

#: `vc check`: what the supervisor is told, and how much of the recent past the line draws on.
_CHECK_REASON = "the user asked for a check right now"
_CHECK_RECENT_LINES = 5

_SAID_LINES = 30
#: How many of the files the agent changed `vc access files` lets the supervisor read.
_WHOLE_FILES = 3

#: Spoken lead-ins so the user can tell the supervisor from the narrator.
_SPOKEN_PREFIX: dict[str, dict[str, str]] = {
    "ru": {"warn": "Внимание:", "stop": "Остановил агента."},
    "en": {"warn": "Heads up:", "stop": "I paused the agent."},
}

_STATUS_WORDS = {"OK": "ok", "WARN": "warn", "STOP": "stop"}


def _format_for_recent(event: Event) -> str:
    return format_events([event]).lstrip("- ")


def _is_shutdown_error(e: BaseException) -> bool:
    """asyncio's default executor is closed once the run ends; a background
    LLM call that lands after that is not an error worth the user's ear."""
    return "shutdown" in str(e).lower()


def _is_correction(event: Event) -> bool:
    """Is this the user talking to a working agent, rather than the agent's next prompt?

    What actually reached the CLI as a prompt arrives as `delivery: observed`.
    """
    payload = event.payload if isinstance(event.payload, dict) else {}
    return event.source == "web" or payload.get("delivery") in ("sent", "delivered")


def _describe_failure(payload: dict[str, Any]) -> tuple[str, str]:
    """(what makes this the same failure, how to name it).

    Shell commands all report the tool name "Bash", so the tool alone would call
    a git error, a missing module and a failed test "the same failure". Numbers
    are dropped to match reruns: durations and line numbers change, the error doesn't.
    Taken from the end, where a test run's verdict and most errors are: long output
    starts with progress dots and truncation notices that look alike whatever failed.
    """
    tool = str(payload.get("tool") or "?")
    text = plain_output(payload.get("preview"))
    key = re.sub(r"\d+", "#", text[-_FAILURE_KEY_CHARS:].lower())
    return f"{tool}:{key}", f"{tool}: {text[-_FAILURE_LABEL_CHARS:]}"


def _same_verdict(prev: tuple[str, str] | None, cur: tuple[str, str]) -> bool:
    """Same status and substantially the same words as the last spoken verdict."""
    if prev is None or prev[0] != cur[0]:
        return False
    a, b = prev[1].lower(), cur[1].lower()
    return SequenceMatcher(None, a, b).ratio() >= 0.6


def parse_supervisor_verdict(text: str) -> tuple[str, str]:
    """Split the supervisor's reply into (status, message).

    The prompt asks for OK / WARN / STOP on the first line; models still wrap
    it in punctuation or bold now and then, so match the word, not the line.
    Anything unrecognisable counts as OK: an unreadable verdict must never
    stop the agent.
    """
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return "ok", ""
    first = lines[0]
    words = first.split(None, 1)
    head = words[0].strip("*_`#:.-").upper() if words else ""
    status = _STATUS_WORDS.get(head)
    if status is None:
        return "ok", ""
    rest = words[1].strip() if len(words) > 1 else ""
    message = " ".join(part for part in (rest, *lines[1:]) if part).strip()
    return status, message


def _provider_key(cfg: CommentatorConfig) -> str:
    """Stable string that changes when provider name or options change."""
    return f"{cfg.provider.name}:{sorted(cfg.provider.options.items())}"


def _truncate_log(s: str, limit: int = 160) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= limit else s[: limit - 1] + "…"
