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
import shutil
import time
from collections import Counter, deque
from collections.abc import Coroutine
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, cast
from uuid import uuid4

from voice_copilot.commentator.casefile import CaseFile, Note, parse_advice
from voice_copilot.commentator.cli_profiles import NARRATION_PROFILES, strong_model_for
from voice_copilot.commentator.format import (
    build_advisor_user,
    build_ask_user,
    build_debrief_user,
    build_narration_user,
    build_summary_user,
    build_supervisor_delta,
    build_supervisor_user,
    edit_excerpt,
    format_events,
    plain_output,
    read_edited_files,
    speech_text,
)
from voice_copilot.commentator.importance import classify, meets_threshold
from voice_copilot.commentator.prompts import load as load_prompt
from voice_copilot.commentator.prompts import load_advisor as load_advisor_prompt
from voice_copilot.commentator.prompts import load_ask as load_ask_prompt
from voice_copilot.commentator.prompts import load_debrief as load_debrief_prompt
from voice_copilot.commentator.prompts import load_summary as load_summary_prompt
from voice_copilot.commentator.prompts import load_supervisor as load_supervisor_prompt
from voice_copilot.core.bus import EventBus
from voice_copilot.core.config import CommentatorConfig, Language
from voice_copilot.core.danger import RULES, Danger, check_tool
from voice_copilot.core.events import Event, EventKind
from voice_copilot.core.user_query import clean_user_query
from voice_copilot.providers import registry
from voice_copilot.providers.llm.base import LLMMessage, LLMProvider
from voice_copilot.providers.llm.live_claude import LiveClaude
from voice_copilot.providers.llm.live_claude import stats as live_stats
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
    #: Which tool call made which of those, so a refused one can be taken back.
    edit_of: dict[str, str] = field(default_factory=dict)
    #: The session's folder, and the files it changed with the latest last.
    cwd: str | None = None
    recent_files: list[str] = field(default_factory=list)
    #: Events on record already: `vc ask` may get to them before the narrator does.
    recorded: set[str] = field(default_factory=set)
    last_event_at: float | None = None
    #: Lines ever added to `history` and edits ever added to `edits`: both are trimmed
    #: from the front, and a running conversation needs to know what it hasn't seen.
    history_total: int = 0
    edits_total: int = 0
    #: Facts about the request in hand, for the end-of-work review and the advisor.
    case: CaseFile = field(default_factory=CaseFile)
    #: The supervisor's word on the turn that just ended, for the review to build on.
    turn_verdict: str | None = None
    #: Advisor: whether it has looked at the plan yet, which sensitive files it has
    #: been asked about, and when it last ran.
    advised_plan: bool = False
    advised_files: set[str] = field(default_factory=set)
    sensitive_due: str | None = None
    advised_at: float = 0.0

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

    def log(self, *lines: str) -> None:
        """Add to the supervisor's transcript: the one way it grows."""
        self.history.extend(lines)
        self.history_total += len(lines)
        if len(self.history) > _SUPERVISOR_HISTORY_LINES:
            self.history = self.history[-_SUPERVISOR_HISTORY_LINES:]

    @property
    def facts(self) -> str | None:
        """What the code knows for certain: the summary when no narrator writes one."""
        parts: list[str] = []
        if self.files_edited:
            names = sorted(self.files_edited)
            more = f" (+{len(names) - 8} more)" if len(names) > 8 else ""
            parts.append(f"Files edited: {', '.join(names[:8])}{more}.")
        failed = sum(self.failures.values())
        if failed:
            parts.append(f"Failed tool calls so far: {failed}.")
        if self.corrections:
            parts.append(f"The user added {len(self.corrections)} instruction(s) along the way.")
        return " ".join(parts) or None

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
        #: By (language, prompt style): the supervisor may run through another CLI.
        self._supervisor_prompts: dict[tuple[str, str], str] = {}
        self._supervisor_tasks: set[asyncio.Task[None]] = set()
        #: Sounds and alarms on their way to the bus, held so the GC doesn't drop them.
        self._side_tasks: set[asyncio.Task[None]] = set()
        #: The supervisor's running conversation per session, where the CLI allows one.
        self._live: dict[str, _LiveReview] = {}

        self._buffer: list[Event] = []
        self._max_buffer = 60
        # When the current buffer started filling — drives the idle timer that
        # speaks up during long stretches of quiet tool work.
        self._buffer_since: float | None = None
        self._speak_task: asyncio.Task[None] | None = None
        # A high-importance event came in while a line was being generated;
        # the follow-up flush must not hold it to the word-count gate.
        self._high_pending = False
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

    @property
    def narrator_on(self) -> bool:
        return self._cfg.narrator

    def latest_open_note(self) -> Note | None:
        """The advisor's latest note the user hasn't acted on, for `vc send`."""
        notes = [n for ctx in self._contexts.values() for n in ctx.case.notes if n.status == "open"]
        return notes[-1] if notes else None

    def gate_policy(self) -> tuple[str, int] | None:
        """Supervisor+'s reaction to a dangerous command, or None when it isn't on."""
        sup = self._cfg.supervisor
        return (sup.react, sup.pause_s) if sup.mode == "plus" else None

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
        sup = self._cfg.supervisor
        strong = strong_model_for(sup.cli or str(provider.options.get("cli") or ""))
        supervisor = sup.model or strong or (f"{sup.cli} default" if sup.cli else narrator)
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
        llm = self._supervisor()
        system = load_ask_prompt(self._language, getattr(llm, "prompt_style", "api"))
        pieces: list[str] = []
        async with asyncio.timeout(_LLM_TIMEOUT_S):
            stream = llm.stream_chat(
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
        # The supervisor LLM is rebuilt lazily at its next checkpoint if the
        # provider or model changed — `_supervisor()` compares keys.

    async def run(self) -> None:
        """Main loop — subscribe to the bus and narrate until cancelled."""
        loop = asyncio.get_event_loop()

        try:
            await self._run_loop(loop)
        finally:
            # A running `claude -p` per session: none may outlive the run.
            await asyncio.gather(
                *(c.live.close() for c in self._live.values()), return_exceptions=True
            )
            self._live.clear()

    async def _run_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        async with self._bus.subscribe() as q:
            while True:
                # Read per batch: a debounce saved in the panel applies at once.
                debounce_s = max(0.05, self._cfg.debounce_ms / 1000.0)
                # Maximum time to keep accumulating events before forcing a
                # flush, even when new events arrive continuously (e.g. long
                # thinking streams): responsive but not spammy.
                max_batch_s = debounce_s * 4
                try:
                    await self._step(q, loop, debounce_s, max_batch_s)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # One malformed event (or a bug it triggers) must not end
                    # narration for the rest of the run.
                    log.exception("commentator: failed on an event, carrying on")

    async def _step(
        self,
        q: asyncio.Queue[Event],
        loop: asyncio.AbstractEventLoop,
        debounce_s: float,
        max_batch_s: float,
    ) -> None:
        """Take the next event (or the idle timeout) and narrate when due."""
        try:
            event = await self._next_event(q)
        except TimeoutError:
            # Only quiet activity for a while. Say something rather
            # than leaving the user listening to nothing.
            if not await self._flush(trigger="idle"):
                self._buffer_since = time.monotonic()
            return
        control = self._consume_control_event(event)
        if control == "flush":
            await self._flush(trigger="playback_ready")
            return
        if control == "check":
            await self._check_now(event)
            return
        if control == "danger":
            await self._on_danger(event)
            return
        if control == "advice":
            await self._on_advice(event)
            return
        if control == "consume":
            return  # USER_MESSAGE etc. — context-only, not narrated
        if not self._cfg.narrator:
            self._watch_only(event)
            return
        admitted = self._admit(event)
        if admitted is None or admitted == "context":
            return
        if admitted == "high":
            await self._flush(trigger="high")
            return

        # Accumulate until the bus goes quiet for debounce_s OR until
        # max_batch_s has elapsed — whichever comes first. The deadline
        # prevents indefinitely deferring narration during long thinking
        # streams where events arrive faster than the debounce window.
        deadline = loop.time() + max_batch_s
        trigger = "normal"
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break  # max batch time reached → flush now
                async with asyncio.timeout(min(debounce_s, remaining)):
                    event = await q.get()
                control = self._consume_control_event(event)
                if control == "flush":
                    await self._flush(trigger="playback_ready")
                    continue
                if control == "check":
                    await self._check_now(event)
                    continue
                if control == "danger":
                    await self._on_danger(event)
                    continue
                if control == "advice":
                    await self._on_advice(event)
                    continue
                if control == "consume":
                    continue
                if self._admit(event) == "high":
                    # Same as a high event arriving first: an edit or a
                    # failure is spoken now, not held to the word gate.
                    trigger = "high"
                    break
        except TimeoutError:
            pass

        await self._flush(trigger=trigger)

    async def _next_event(self, q: asyncio.Queue[Event]) -> Event:
        """Next bus event, or raise TimeoutError when the idle window expires.

        Queue waits here and in `_step` use `asyncio.timeout`, not `wait_for`:
        on Python 3.11 `wait_for` drops a cancellation that arrives as the
        queue hands over an event, and the narrator kept running after stop.
        """
        async with asyncio.timeout(self._idle_timeout()):
            return await q.get()

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

        if event.kind in (EventKind.SUPERVISOR_DANGER, EventKind.SUPERVISOR_DANGER_DECIDED):
            return "danger"

        if event.kind is EventKind.USER_ADVICE:
            return "advice"

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
            self._end_live(only)
            return "consume"

        if event.kind is EventKind.SESSION_ENDED:
            self._end_live(self._session_key_of(event))
            return None

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
            if key == _NO_SESSION_KEY and _is_correction(event) and self._sessions is not None:
                # The panel and `vc send` name no session: the message goes to the agent the
                # panel follows, so that is where it belongs on record. Not on a session of
                # its own, where it would be a new task nobody works on.
                key = self._sessions.get_active_id() or key
            ctx = self._contexts.setdefault(key, _SessionContext())
            # Through the proxy, the user's addition comes back as the latest message of the
            # agent's next request. It is still an addition, not a new task.
            if ctx.user_query and (_is_correction(event) or text in ctx.corrections):
                # Said to a working agent: it changes the task, it doesn't replace it.
                # As the new "goal", "skip the README" would make the rest of the work
                # look unrelated, and the supervisor would stop the agent for it.
                if text != ctx.user_query and text not in ctx.corrections:
                    ctx.corrections.append(text)
                    ctx.log(f"- the user said: {text}")
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
                ctx.case = CaseFile()
                self._end_live(key)  # a new task: the conversation starts over with it
                ctx.advised_plan, ctx.advised_files, ctx.sensitive_due = False, set(), None
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
        self._danger_seen(event)
        imp = classify(event, self._cfg)
        if imp is None:
            self._intake_for_supervisor(event)
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

    def _intake_for_supervisor(self, event: Event) -> None:
        """What the narrator was told not to mention still happened: the supervisor gets it."""
        if self._cfg.supervisor.mode == "off" or event.source.startswith("commentator"):
            return
        if classify(event, _SEE_ALL) is None or not self._accepts(event):
            return
        self._record_events(self._session_key_of(event), [event])

    def _watch_only(self, event: Event) -> None:
        """Supervisor+ alone: nothing is narrated, every event goes straight on its record."""
        self._danger_seen(event)
        if event.source.startswith("commentator") or not self._accepts(event):
            return
        if classify(event, _SEE_ALL) is None:
            return
        key = self._session_key_of(event)
        ctx = self._contexts.setdefault(key, _SessionContext())
        ctx.last_event_at = time.monotonic()
        self._review_if_due(
            _Batch(
                events=[event],
                session_key=key,
                user_query=ctx.goal,
                summary=ctx.summary,
                query_version=ctx.query_version,
            )
        )

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
            if trigger == "high":
                self._high_pending = True
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
        self._high_pending = False
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
            if self._cfg.narrator and not self._review_speaks_for(batch):
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
                await self._flush(trigger="high" if self._high_pending else "normal")

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
            language=self._language,
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

    async def _review(self, batch: _Batch, reason: str, ctx: _SessionContext) -> None:
        await self._supervise(batch, reason)
        await self._debrief_if_due(batch, reason)
        due, ctx.final_review_due = ctx.final_review_due, None
        if due is not None and due.query_version == ctx.query_version:
            await self._supervise(due, "turn ended")
            await self._debrief_if_due(due, "turn ended")

    def _review_if_due(self, batch: _Batch) -> None:
        """Add the batch to the supervisor's transcript; review at a checkpoint.

        Runs whether or not the narration worked: a narrator that errors out or
        comes back empty must not quietly switch the supervisor off too.
        """
        if self._is_stale_batch(batch):
            return
        ctx = self._contexts.setdefault(batch.session_key, _SessionContext())
        self._record_for_supervisor(batch)
        self._advise_if_due(batch, ctx)
        reason = self._checkpoint_reason(batch, ctx)
        if reason is None:
            return
        review = asyncio.create_task(
            self._review(batch, reason, ctx),
            name=f"commentator.supervisor:{batch.session_key}",
        )
        self._supervisor_tasks.add(review)
        review.add_done_callback(self._supervisor_tasks.discard)

    def _record_for_supervisor(self, batch: _Batch) -> None:
        self._record_events(batch.session_key, batch.events)

    def _record_events(self, session_key: str, events: list[Event]) -> None:
        """Put these on record, once each, as soon as anything may ask about them."""
        ctx = self._contexts.setdefault(session_key, _SessionContext())
        fresh = [
            ev for ev in events if ev.kind is not EventKind.USER_CHECK and ev.id not in ctx.recorded
        ]
        ctx.recorded.update(ev.id for ev in fresh)
        ctx.log(*format_events(fresh).splitlines())
        for ev in fresh:
            ctx.case.tick()
            tool_id = ev.payload.get("tool_use_id") if isinstance(ev.payload, dict) else None
            if ev.kind is EventKind.TOOL_CALL_STARTED:
                ctx.tools_since_review += 1
                ctx.tool_calls += 1
                edit = edit_excerpt(ev.payload)
                if edit:
                    ctx.edits = [*ctx.edits, edit][-_SUPERVISOR_EDITS:]
                    ctx.edits_total += 1
                    ctx.case.edited()
                    if tool_id:
                        ctx.edit_of[str(tool_id)] = edit
                tool_input = ev.payload.get("input")
                if isinstance(tool_input, dict):
                    command = tool_input.get("command") or tool_input.get("cmd")
                    ctx.case.started(str(tool_id) if tool_id else None, command)
            elif ev.kind is EventKind.FILE_EDITED:
                path = str(ev.payload.get("path"))
                ctx.files_edited.add(path)
                ctx.recent_files.append(path)
                ctx.case.edited()
                if _SENSITIVE_PATH.search(path) and path not in ctx.advised_files:
                    ctx.sensitive_due = path
            elif ev.kind is EventKind.TOOL_CALL_FINISHED:
                ctx.case.finished(
                    str(tool_id) if tool_id else None, bool(ev.payload.get("is_error"))
                )
            if ev.kind is EventKind.TOOL_CALL_FINISHED and ev.payload.get("is_error"):
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
        turn_over = _turn_is_over(batch.events)
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
        """The supervisor's LLM: the narrator's provider with a stronger model, or another
        CLI on its own login when the user picked one."""
        sup = self._cfg.supervisor
        provider = self._cfg.provider
        name = provider.name
        options: dict[str, str | int | float | bool]
        if sup.cli:
            name, options = "auto", {"cli": sup.cli}
            binary = shutil.which(sup.cli)
            if binary:
                options["binary"] = binary  # missing: the provider says so when called
            model = sup.model or strong_model_for(sup.cli)
        else:
            options = dict(provider.options)
            model = sup.model or (
                strong_model_for(str(options.get("cli") or "")) if name == "auto" else None
            )
        if model:
            options["model"] = model
        key = f"{name}:{sorted(options.items())}"
        if self._supervisor_llm is None or key != self._supervisor_key:
            self._supervisor_llm = cast(LLMProvider, registry.build("llm", name, options))
            self._supervisor_key = key
            log.info(
                "supervisor: using %s%s model=%s",
                name,
                f" ({sup.cli})" if sup.cli else "",
                options.get("model", "<default>"),
            )
        return self._supervisor_llm

    def _supervisor_system(self, llm: LLMProvider) -> str:
        style = getattr(llm, "prompt_style", "api")
        key = (self._language, style)
        if key not in self._supervisor_prompts:
            self._supervisor_prompts[key] = load_supervisor_prompt(self._language, style)
        return self._supervisor_prompts[key]

    async def _supervise(self, batch: _Batch, reason: str) -> None:
        ctx = self._contexts.setdefault(batch.session_key, _SessionContext())
        if reason == "turn ended":
            # This turn's word, or none: a review that fails must not hand on the last one's OK.
            ctx.turn_verdict = None
        ctx.tools_since_review = 0
        edits, files = self._files_for_models(ctx)
        user_content = build_supervisor_user(
            user_query=batch.user_query,
            summary=ctx.summary or batch.summary or ctx.facts,
            history=ctx.history,
            reason=reason,
            style=self._prompt_style,
            already_said=ctx.last_verdict[1] if ctx.last_verdict else None,
            progress=ctx.progress,
            edits=edits,
            files=files,
            language=self._language,
        )
        session_payload = (
            {"session_id": batch.session_key} if batch.session_key != _NO_SESSION_KEY else {}
        )
        log.info("supervisor: review (%s) for session=%s", reason, batch.session_key)
        pieces: list[str] = []
        system = ""
        try:
            if self._live_binary() is not None:
                async with asyncio.timeout(_LLM_TIMEOUT_S):
                    pieces.append(
                        await self._live_review(batch.session_key, ctx, user_content, reason)
                    )
            else:
                # Off the event loop: building a provider can read the keychain or
                # run `gh auth token`, and the proxy shares this loop.
                llm = await asyncio.to_thread(self._supervisor)
                system = self._supervisor_system(llm)
                async with asyncio.timeout(_LLM_TIMEOUT_S):
                    stream = llm.stream_chat(
                        [LLMMessage(role="user", content=user_content)],
                        system=system,
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
        if system:
            self._count("supervisor", system + user_content, reply)
        status, message = parse_supervisor_verdict(reply)
        if ctx.query_version != batch.query_version:
            log.info("supervisor: dropping stale verdict for session=%s", batch.session_key)
            return
        sup = self._cfg.supervisor
        # Supervisor+ acts on a STOP the way the user chose; the plain supervisor only says it.
        reaction = sup.react if sup.mode == "plus" else "say"
        acted = status == "stop" and reaction in ("hold", "pause")
        # The same complaint at every checkpoint is nagging, not oversight:
        # a verdict that restates the last spoken one goes to the Trace only.
        repeat = (
            status != "ok"
            and not reason.startswith(_CHECK_REASON)  # They asked: "as I said" is still an answer.
            and _same_verdict(ctx.last_verdict, (status, message))
        )
        if status != "ok":
            ctx.last_verdict = (status, message)
        if reason == "turn ended":
            ctx.turn_verdict = "OK" if status == "ok" else f"{status.upper()}: {message}"
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
                    "reaction": reaction,
                    "repeat": repeat,
                    "query_version": batch.query_version,
                    **session_payload,
                },
            )
        )
        plus = sup.mode == "plus"
        if status == "ok" or not message or (repeat and not acted):
            if plus and reason == "turn ended" and ctx.tool_calls and not sup.debrief:
                await self._cue("done", batch.session_key)  # with a review, it chimes first
            return
        ctx.case.told.append(f"{status.upper()}: {message}")
        if acted:
            await self._bus.publish(
                Event(
                    kind=EventKind.SUPERVISOR_STOP,
                    source="commentator.supervisor",
                    payload={
                        "message": message,
                        "resume_after_s": sup.pause_s if reaction == "pause" else None,
                        **session_payload,
                    },
                )
            )
        if plus:
            await self._cue("critical" if status == "stop" else "warn", batch.session_key)
        spoken = _SPOKEN_PREFIX.get(self._language, _SPOKEN_PREFIX["en"])
        lead = (
            spoken["warn"]
            if not acted
            else spoken["pause"].format(n=sup.pause_s)
            if reaction == "pause"
            else spoken["stop"]
        )
        await self._emit(
            {
                "utterance_id": f"sup-{batch.events[-1].id}",
                "text": f"{lead} {message}",
                "read_text": speech_text(f"{lead} {message}"),
                "streaming": False,
                "language": self._language,
                "role": "supervisor",
                "urgent": plus,
                "query_version": batch.query_version,
                **session_payload,
            }
        )

    # ------------------------------------------------- the running conversation

    def _live_binary(self) -> str | None:
        """The `claude` to keep a conversation with, or None: one-shot reviews then.

        Only through the Claude CLI: it is the one measured, and the one that takes a
        conversation on stdin. Other CLIs and the API providers review one-shot.
        """
        sup = self._cfg.supervisor
        if not sup.keep_session:
            return None
        provider = self._cfg.provider
        through = sup.cli or (
            str(provider.options.get("cli") or "") if provider.name == "auto" else ""
        )
        if through != "claude":
            return None
        return shutil.which("claude") or "claude"  # not found: the start fails, out loud

    async def _live_review(
        self, session_key: str, ctx: _SessionContext, full: str, reason: str
    ) -> str:
        """One review in the session's running conversation: only what is new goes out.

        The first message is the one-shot review's, word for word. A new task, a long
        pause (the cache has gone cold), a conversation grown large, a changed model or
        language, or a turn that didn't finish: each starts over with a full message.
        """
        sup = self._cfg.supervisor
        model = sup.model or strong_model_for("claude")
        key = f"{model}|{self._language}|{self._cfg.file_access}"
        conv = self._live.get(session_key)
        why = None
        if conv is not None:
            why = (
                "the model, language or access changed"
                if conv.key != key
                else "a new task"
                if conv.query_version != ctx.query_version
                else "its process ended"
                if not conv.live.alive
                else "a long pause"
                if time.monotonic() - conv.live.last_turn_at > _LIVE_IDLE_S
                else "it grew large"
                if conv.live.context_tokens > _LIVE_BUDGET_TOKENS
                else None
            )
        if conv is not None and why is not None:
            log.info("supervisor: starting the conversation over for %s: %s", session_key, why)
            self._end_live(session_key)
            conv = None
        if conv is None:
            binary = self._live_binary() or "claude"
            conv = _LiveReview(
                live=LiveClaude(
                    binary,
                    system=load_supervisor_prompt(self._language, "cli"),
                    model=model,
                    args=NARRATION_PROFILES["claude"].args,
                ),
                key=key,
                query_version=ctx.query_version,
            )
            self._live[session_key] = conv
            content = full
        else:
            unsent = ctx.history_total - conv.sent_total
            new_lines = ctx.history[max(0, len(ctx.history) - unsent) :]
            if unsent > len(ctx.history):
                # More happened than the transcript keeps: the latest part, and say so.
                new_lines = [
                    f"- (… {unsent - len(ctx.history)} earlier lines not shown)",
                    *new_lines,
                ]
            new_edits = (
                ctx.edits[max(0, len(ctx.edits) - (ctx.edits_total - conv.edits_sent)) :]
                if self._cfg.file_access != "events"
                else []
            )
            told = ctx.last_verdict[1] if ctx.last_verdict else None
            content = build_supervisor_delta(
                new_lines=new_lines,
                reason=reason,
                progress=ctx.progress,
                already_said=told if told != conv.told else None,
                edits=new_edits,
                language=self._language,
            )
        try:
            reply = await conv.live.turn(content)
        except BaseException:
            # Whatever happened, this conversation can't go on: the next review starts over.
            if self._live.get(session_key) is conv:
                self._end_live(session_key)
            raise
        conv.sent_total, conv.edits_sent = ctx.history_total, ctx.edits_total
        conv.told = ctx.last_verdict[1] if ctx.last_verdict else None
        for field_name, name in (
            ("cache_read_input_tokens", "cache_read"),
            ("cache_creation_input_tokens", "cache_write"),
            ("input_tokens", "uncached"),
        ):
            self._usage[f"supervisor.{name}"] += reply.usage.get(field_name, 0)
        log.info(
            "supervisor: live turn %d for session=%s (%s)",
            conv.live.turns,
            session_key,
            live_stats(reply.usage),
        )
        self._count("supervisor", content, reply.text)
        return reply.text

    def _end_live(self, session_key: str | None) -> None:
        """Close the running conversation of this session, or of all of them."""
        for key in [k for k in self._live if session_key in (None, k)]:
            self._spawn(self._live.pop(key).live.close())

    # ------------------------------------------------------- the review at the end

    def _review_speaks_for(self, batch: _Batch) -> bool:
        """Supervisor+'s review will close this turn, so the narrator's closing line would
        only say the same twice: the end of the work has one voice."""
        sup = self._cfg.supervisor
        if sup.mode != "plus" or not sup.debrief or not _turn_is_over(batch.events):
            return False
        ctx = self._contexts.get(batch.session_key)
        return ctx is not None and ctx.tool_calls > 0

    async def _debrief_if_due(self, batch: _Batch, reason: str) -> None:
        """When the work is done: the chime, then a short spoken review."""
        sup = self._cfg.supervisor
        ctx = self._contexts.setdefault(batch.session_key, _SessionContext())
        if reason != "turn ended" or sup.mode != "plus" or not sup.debrief or not ctx.tool_calls:
            return
        if ctx.query_version != batch.query_version:
            return
        edits, _ = self._files_for_models(ctx)
        user_content = build_debrief_user(
            user_query=ctx.goal,
            case_file=self._case_file(ctx),
            verdict=ctx.turn_verdict,
            history=ctx.history,
            edits=edits,
            language=self._language,
        )
        reply = await self._ask_strong(
            "debrief", user_content, load_debrief_prompt, batch.session_key, max_tokens=300
        )
        if reply is None or ctx.query_version != batch.query_version:
            return
        log.info("debrief: %s", _truncate_log(reply))
        if ctx.turn_verdict == "OK":
            await self._cue("done", batch.session_key)
        await self._emit(
            {
                "utterance_id": f"debrief-{batch.events[-1].id}",
                "text": reply,
                "read_text": speech_text(reply),
                "streaming": False,
                "language": self._language,
                "role": "supervisor",
                "kind": "debrief",
                "urgent": True,
                "query_version": batch.query_version,
                **(
                    {"session_id": batch.session_key}
                    if batch.session_key != _NO_SESSION_KEY
                    else {}
                ),
            }
        )

    def _case_file(self, ctx: _SessionContext) -> str:
        return ctx.case.render(goal=ctx.goal, progress=ctx.progress, files=sorted(ctx.files_edited))

    # ------------------------------------------------------------- the advisor

    def _advise_if_due(self, batch: _Batch, ctx: _SessionContext) -> None:
        """The advisor looks when the agent commits to an approach, and when it touches a
        file where mistakes cost more. Not at every checkpoint: a note interrupts."""
        sup = self._cfg.supervisor
        if sup.mode != "plus" or sup.advice == "off" or not ctx.files_edited:
            return
        if self._is_stale_batch(batch) or self._advice_in_flight(batch.session_key):
            return
        if not ctx.advised_plan:
            ctx.advised_plan = True
            reason = "plan: the agent started changing files"
        elif ctx.sensitive_due and time.monotonic() - ctx.advised_at >= _ADVICE_GAP_S:
            ctx.advised_files.add(ctx.sensitive_due)
            reason = f"an edit to a sensitive file: {ctx.sensitive_due}"
        else:
            return
        ctx.sensitive_due = None
        ctx.advised_at = time.monotonic()
        task = asyncio.create_task(
            self._advise(batch, reason, ctx), name=f"commentator.advisor:{batch.session_key}"
        )
        self._supervisor_tasks.add(task)
        task.add_done_callback(self._supervisor_tasks.discard)

    def _advice_in_flight(self, session_key: str) -> bool:
        return any(
            not t.done() and t.get_name() == f"commentator.advisor:{session_key}"
            for t in self._supervisor_tasks
        )

    async def _advise(self, batch: _Batch, reason: str, ctx: _SessionContext) -> None:
        edits, _ = self._files_for_models(ctx)
        user_content = build_advisor_user(
            user_query=ctx.goal,
            case_file=self._case_file(ctx),
            watch=ctx.case.watch,
            raised=[f"({n.status}) {n.text}" for n in ctx.case.notes],
            reason=reason,
            history=ctx.history,
            edits=edits,
            language=self._language,
        )
        reply = await self._ask_strong(
            "advisor", user_content, load_advisor_prompt, batch.session_key, max_tokens=400
        )
        if reply is None or ctx.query_version != batch.query_version:
            return
        watch, text, send = parse_advice(reply)
        if watch:
            ctx.case.watch = watch
        log.info("advisor (%s): %s", reason, text or "OK")
        if not text or any(_same_note(n.text, text) for n in ctx.case.notes):
            return  # nothing, or what the user has heard already
        note = Note(id=uuid4().hex[:12], text=text, send=send)
        ctx.case.notes.append(note)
        session_payload = (
            {"session_id": batch.session_key} if batch.session_key != _NO_SESSION_KEY else {}
        )
        await self._cue("advice", batch.session_key)
        await self._bus.publish(
            Event(
                kind=EventKind.SUPERVISOR_NOTE,
                source="commentator.advisor",
                payload={
                    "id": note.id,
                    "text": text,
                    "send": send,
                    "reason": reason,
                    **session_payload,
                },
            )
        )
        if send and self._cfg.supervisor.advice == "auto":
            await self._send_note(note, batch.session_key)

    async def _on_advice(self, event: Event) -> None:
        """The user sends a note's instruction to the agent, or puts the note away."""
        payload = event.payload if isinstance(event.payload, dict) else {}
        wanted = payload.get("id")
        found = [
            (key, note)
            for key, ctx in self._contexts.items()
            for note in ctx.case.notes
            if (note.id == wanted if wanted else note.status == "open")
        ]
        if not found:
            return
        key, note = found[-1]  # `vc send` with no id: the latest open note
        if payload.get("action") == "send" and note.send:
            await self._send_note(note, key)
        elif payload.get("action") == "dismiss":
            note.status = "dismissed"
            await self._note_decided(note, key)

    async def _send_note(self, note: Note, session_key: str) -> None:
        note.status = "sent"
        sid = {"session_id": session_key} if session_key != _NO_SESSION_KEY else {}
        # The user's own message, as one typed in the panel: the same route to the agent.
        await self._bus.publish(
            Event(
                kind=EventKind.USER_MESSAGE,
                source="web.advice",
                payload={"text": note.send, **sid},
            )
        )
        await self._note_decided(note, session_key)

    async def _note_decided(self, note: Note, session_key: str) -> None:
        await self._bus.publish(
            Event(
                kind=EventKind.SUPERVISOR_NOTE_DECIDED,
                source="commentator.advisor",
                payload={
                    "id": note.id,
                    "status": note.status,
                    **({"session_id": session_key} if session_key != _NO_SESSION_KEY else {}),
                },
            )
        )

    async def _ask_strong(
        self,
        where: str,
        user_content: str,
        load: Any,
        session_key: str,
        *,
        max_tokens: int,
    ) -> str | None:
        """One call to the supervisor's model with this prompt. Errors go to the panel."""
        sid = {"session_id": session_key} if session_key != _NO_SESSION_KEY else {}

        async def report(message: str) -> None:
            await self._bus.publish(
                Event(
                    kind=EventKind.ERROR,
                    source=f"commentator.{where}",
                    payload={"where": where, "message": message, **sid},
                )
            )

        pieces: list[str] = []
        system = ""
        try:
            llm = await asyncio.to_thread(self._supervisor)
            system = load(self._language, getattr(llm, "prompt_style", "api"))
            async with asyncio.timeout(_LLM_TIMEOUT_S):
                stream = llm.stream_chat(
                    [LLMMessage(role="user", content=user_content)],
                    system=system,
                    max_tokens=max_tokens,
                    temperature=0.2,
                )
                async for delta in stream:
                    if delta:
                        pieces.append(delta)
        except TimeoutError:
            await report(f"The {where}'s model didn't answer in {_LLM_TIMEOUT_S:.0f} s.")
            return None
        except RuntimeError as e:
            if not _is_shutdown_error(e):
                log.exception("%s LLM error", where)
                await report(str(e))
            return None
        except Exception as e:
            log.exception("%s LLM error", where)
            await report(str(e))
            return None
        reply = "".join(pieces).strip()
        if not reply:
            await report(f"The {where}'s model returned no text.")
            return None
        self._count(where, system + user_content, reply)
        return reply

    # ------------------------------------------------------------- danger

    def _danger_seen(self, event: Event) -> None:
        """Supervisor+ outside plugin hooks: a dangerous command can only be called out.

        The proxy sees the call as the model asks for it, a spawned CLI as it starts:
        either way there is nothing to hold, so the reaction is to say so.
        """
        if event.kind is not EventKind.TOOL_CALL_STARTED or self._cfg.supervisor.mode != "plus":
            return
        if event.source.startswith("hooks."):
            return  # the hook held it already, and reported it
        payload = event.payload if isinstance(event.payload, dict) else {}
        ctx = self._contexts.get(self._session_key_of(event))
        danger = check_tool(payload.get("tool"), payload.get("input"), ctx.cwd if ctx else None)
        if danger is None:
            return
        sid = payload.get("session_id")
        self._spawn(
            self._bus.publish(
                Event(
                    kind=EventKind.SUPERVISOR_DANGER,
                    source="commentator.supervisor",
                    payload={
                        "id": str(payload.get("tool_use_id") or event.id),
                        "rule": danger.rule,
                        "excerpt": danger.excerpt,
                        "reaction": "say",
                        "wait_s": None,
                        "before_run": False,
                        **({"session_id": sid} if sid else {}),
                    },
                )
            )
        )

    async def _on_danger(self, event: Event) -> None:
        """Say a caught command out loud, whichever session it came from, and log the outcome."""
        payload = event.payload if isinstance(event.payload, dict) else {}
        rule = str(payload.get("rule") or "")
        if rule not in RULES:
            return
        danger = Danger(rule, str(payload.get("excerpt") or ""))
        key = self._session_key_of(event)
        ctx = self._contexts.setdefault(key, _SessionContext())
        # The call itself may still wait in the narrator's buffer: on record first, so the
        # log reads in the order things happened.
        self._record_events(key, [ev for ev in self._buffer if self._session_key_of(ev) == key])
        if event.kind is EventKind.SUPERVISOR_DANGER_DECIDED:
            ctx.log(_decided_line(danger.excerpt, payload))
            ctx.case.held.append(_decided_line(danger.excerpt, payload).removeprefix("- "))
            refused_edit = ctx.edit_of.pop(str(payload.get("id")), None)
            if not payload.get("allowed") and refused_edit in ctx.edits:
                ctx.edits.remove(refused_edit)  # it never happened
            return
        ctx.log(
            f"- Supervisor+ caught a dangerous command ({danger.label('en')}): {danger.excerpt}"
        )
        reaction = str(payload.get("reaction") or "say")
        wait = payload.get("wait_s")
        phrases = _DANGER_SPOKEN.get(self._language, _DANGER_SPOKEN["en"])
        text = phrases[reaction if reaction in ("hold", "pause") else "say"].format(
            what=danger.label(self._language),
            n=round(wait) if isinstance(wait, (int, float)) else 0,
        )
        await self._cue("critical", key)
        await self._emit(
            {
                "utterance_id": f"danger-{payload.get('id') or event.id}",
                "text": text,
                "read_text": speech_text(text),
                "streaming": False,
                "language": self._language,
                "role": "supervisor",
                "urgent": True,
                "query_version": ctx.query_version,
                **({"session_id": key} if key != _NO_SESSION_KEY else {}),
            }
        )

    async def _cue(self, name: str, session_key: str) -> None:
        """A sound for the panel: the alarm, a warning, the end of the work."""
        await self._bus.publish(
            Event(
                kind=EventKind.CUE,
                source="commentator.supervisor",
                payload={
                    "cue": name,
                    **({"session_id": session_key} if session_key != _NO_SESSION_KEY else {}),
                },
            )
        )

    def _spawn(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(work, name="commentator.side")
        self._side_tasks.add(task)
        task.add_done_callback(self._side_tasks.discard)

    # ------------------------------------------------------------- summary

    async def _update_summary(self, batch: _Batch, narration: str) -> None:
        """Roll the running summary forward: prev + new events + what we said → new summary."""
        user_content = build_summary_user(
            prev_summary=batch.summary,
            events=batch.events,
            narration=narration,
            style=self._prompt_style,
            language=self._language,
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

#: The running conversation starts over after this long without a review. Claude Code's
#: prompt cache is the one-hour kind (measured: a turn after 21 idle minutes still read the
#: whole conversation from it); past its life the next turn would write the whole
#: conversation again, dearer than a fresh full message.
_LIVE_IDLE_S = 50 * 60.0
#: ...and when it has grown this big, as the model counts it.
_LIVE_BUDGET_TOKENS = 60_000


@dataclass
class _LiveReview:
    live: LiveClaude
    key: str
    query_version: int
    #: How far into `history_total` / `edits_total` the conversation has been told.
    sent_total: int = 0
    edits_sent: int = 0
    #: The warning it was last shown as "already told", so it isn't shown again.
    told: str | None = None


_SUPERVISOR_EDITS = 6

#: `vc check`: what the supervisor is told, and how much of the recent past the line draws on.
_CHECK_REASON = "the user asked for a check right now"
_CHECK_RECENT_LINES = 5

_SAID_LINES = 30
#: How many of the files the agent changed `vc access files` lets the supervisor read.
_WHOLE_FILES = 3

#: Spoken lead-ins so the user can tell the supervisor from the narrator.
_SPOKEN_PREFIX: dict[str, dict[str, str]] = {
    "ru": {"warn": "Внимание:", "stop": "Остановил агента.", "pause": "Пауза на {n} секунд."},
    "en": {
        "warn": "Heads up:",
        "stop": "I paused the agent.",
        "pause": "Pausing the agent for {n} seconds.",
    },
    "uk": {"warn": "Увага:", "stop": "Я зупинив агента.", "pause": "Пауза на {n} секунд."},
    "es": {
        "warn": "Atención:",
        "stop": "He pausado al agente.",
        "pause": "Pauso al agente {n} segundos.",
    },
    "fr": {
        "warn": "Attention :",
        "stop": "J'ai mis l'agent en pause.",
        "pause": "Je mets l'agent en pause {n} secondes.",
    },
}

#: What Supervisor+ says about a dangerous command, by reaction.
_DANGER_SPOKEN: dict[str, dict[str, str]] = {
    "ru": {
        "hold": "Опасная команда: {what}. Агент ждёт вашего решения в панели.",
        "pause": "Опасная команда: {what}. Агент ждёт {n} секунд, отменить можно в панели.",
        "say": "Внимание, опасная команда: {what}.",
    },
    "en": {
        "hold": "Dangerous command: {what}. The agent waits for your answer in the panel.",
        "pause": "Dangerous command: {what}. The agent waits {n} seconds; you can stop it in the panel.",
        "say": "Heads up, a dangerous command: {what}.",
    },
    "uk": {
        "hold": "Небезпечна команда: {what}. Агент чекає на ваше рішення в панелі.",
        "pause": "Небезпечна команда: {what}. Агент чекає {n} секунд, скасувати можна в панелі.",
        "say": "Увага, небезпечна команда: {what}.",
    },
    "es": {
        "hold": "Comando peligroso: {what}. El agente espera tu respuesta en el panel.",
        "pause": "Comando peligroso: {what}. El agente espera {n} segundos; puedes pararlo en el panel.",
        "say": "Atención, un comando peligroso: {what}.",
    },
    "fr": {
        "hold": "Commande dangereuse : {what}. L'agent attend votre réponse dans le panneau.",
        "pause": "Commande dangereuse : {what}. L'agent attend {n} secondes ; vous pouvez l'arrêter dans le panneau.",
        "say": "Attention, une commande dangereuse : {what}.",
    },
}

#: Files where a mistake costs more: the advisor takes a look when the agent edits one.
_SENSITIVE_PATH = re.compile(
    r"auth|login|passw|session|token|secret|crypt|secur|permission|acl|payment|billing"
    r"|stripe|migrat|schema|\.sql$|dockerfile|docker-compose|\.github/workflows|\.gitlab-ci"
    r"|jenkinsfile|deploy|terraform|\.tf$|requirements[^/]*\.txt$|pyproject\.toml$"
    r"|package\.json$|cargo\.toml$|go\.mod$|gemfile$|settings\.py$|\.env",
    re.IGNORECASE,
)
#: The advisor after the plan: at most once in this long, so a burst of edits to config
#: files doesn't become a burst of notes.
_ADVICE_GAP_S = 60.0


def _same_note(a: str, b: str) -> bool:
    """A note that says again what was said already, in other words."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio() >= 0.6


#: The supervisor's intake when no narrator filters it: everything a narrator could say.
_SEE_ALL = CommentatorConfig(speak_tool_calls=True, speak_thinking=True, speak_file_edits=True)


def _turn_is_over(events: list[Event]) -> bool:
    """The agent's own turn ended: not a sub-agent's, not a step between tool calls."""
    return any(
        ev.kind is EventKind.TURN_ENDED
        and ev.payload.get("final") is not False
        and not ev.payload.get("subagent")
        for ev in events
    )


def _decided_line(excerpt: str, payload: dict[str, Any]) -> str:
    """How the user's answer on a held command reads in the supervisor's log."""
    allowed, by = bool(payload.get("allowed")), payload.get("by")
    if allowed:
        if by == "timeout":
            return f"- nobody stopped it during the pause, so it ran: {excerpt}"
        return f"- the user said: run it anyway ({excerpt})"
    if by == "timeout":
        return f"- nobody answered about it, so it did not run: {excerpt}"
    return f"- the user said: don't run it ({excerpt}); it did not run"


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
