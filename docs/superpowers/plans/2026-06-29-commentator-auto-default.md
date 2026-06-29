# Commentator `auto` Default Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the commentator default to `auto` — generating narration by shelling out to the same CLI the user launched via `vc` (its own auth, no keys), with a global auto/api toggle, optional per-CLI overrides, and an always-on panel status indicator.

**Architecture:** A new `auto` LLM provider reuses the existing `copilot-cli` subprocess pattern (`prompt_style="cli"`), driven by a per-CLI narration-profile table. `vc` resolves the launched CLI and, when the effective commentator mode is `current`, builds the commentator against `AutoCommentatorProvider(cli, binary)`. A shared `_cli_runner` is extracted from `copilot_cli.py` so both providers share one subprocess runner.

**Tech Stack:** Python 3.11+, existing FastAPI/asyncio stack, pydantic config, pytest + pytest-asyncio. No new dependencies.

## Global Constraints

- Python 3.11+, formatted with `ruff format`, linted with `ruff`, `mypy` strict (CI runs `uv run mypy src/voice_copilot` on the whole package — keep it clean on both default and `--platform linux`).
- Package manager `uv` (`uv run pytest`, etc.). Ignore the harmless `VIRTUAL_ENV=... does not match` uv warning.
- No hidden retries, no silent fallbacks — a failed/empty narration LLM call must surface as an `ERROR` event and/or panel status, never a silent no-op.
- Default to no comments; only where the *why* isn't obvious.
- Prefer editing existing files over creating new ones; new files only for genuinely new responsibilities.
- Narration uses each CLI's own auth — never extract/forward tokens.

---

### Task 1: Config model — `mode` + `per_cli` overrides

**Files:**
- Modify: `src/voice_copilot/core/config.py` (add `CommentatorCliOverride`, extend `CommentatorConfig`)
- Modify: `tests/unit/test_config.py` (append tests)

**Interfaces:**
- Produces:
  ```python
  class CommentatorCliOverride(BaseModel):
      mode: Literal["current", "api"]
      model: str | None = None

  # CommentatorConfig gains:
  #   mode: Literal["auto", "api"] = "auto"
  #   per_cli: dict[str, CommentatorCliOverride] = {}
  ```

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_config.py`:

```python
def test_commentator_mode_defaults_to_auto(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "missing.yaml")
    assert cfg.commentator.mode == "auto"
    assert cfg.commentator.per_cli == {}


def test_commentator_mode_and_per_cli_round_trip(tmp_path: Path) -> None:
    from voice_copilot.core.config import CommentatorCliOverride

    config_file = tmp_path / "config.yaml"
    cfg = load_config(config_file)
    cfg.commentator.mode = "api"
    cfg.commentator.per_cli = {
        "gemini": CommentatorCliOverride(mode="api", model="gemini-2.0-flash")
    }
    save_config(cfg, config_file)
    reloaded = load_config(config_file)
    assert reloaded.commentator.mode == "api"
    assert reloaded.commentator.per_cli["gemini"].mode == "api"
    assert reloaded.commentator.per_cli["gemini"].model == "gemini-2.0-flash"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_config.py -k "commentator_mode" -v`
Expected: FAIL with `AttributeError: 'CommentatorConfig' object has no attribute 'mode'`

- [ ] **Step 3: Write minimal implementation**

In `src/voice_copilot/core/config.py`, add this class immediately before `class CommentatorConfig(BaseModel):`:

```python
class CommentatorCliOverride(BaseModel):
    mode: Literal["current", "api"]
    model: str | None = None
```

Then add two fields to `CommentatorConfig`, right after the `provider: ProviderConfig = ...` field:

```python
    mode: Literal["auto", "api"] = "auto"
    per_cli: dict[str, CommentatorCliOverride] = Field(default_factory=dict)
```

(`Literal` and `Field` are already imported in this module.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_config.py -v`
Expected: PASS (all, including the two new tests)

- [ ] **Step 5: Run gates and commit**

```bash
uv run ruff check src/voice_copilot/core/config.py tests/unit/test_config.py
uv run ruff format src/voice_copilot/core/config.py
uv run mypy src/voice_copilot
git add src/voice_copilot/core/config.py tests/unit/test_config.py
git commit -m "feat: add commentator mode (auto/api) + per-CLI overrides to config"
```

---

### Task 2: Shared CLI subprocess runner

**Files:**
- Create: `src/voice_copilot/providers/llm/_cli_runner.py`
- Modify: `src/voice_copilot/providers/llm/copilot_cli.py` (use the shared runner + shared prompt builder)
- Test: `tests/unit/test_cli_runner.py`

**Interfaces:**
- Produces:
  ```python
  def run_cli(cmd: list[str], *, stdin_text: str | None = None, timeout: float = 60.0) -> tuple[str, str]:
      """Run cmd to completion; if stdin_text is given, feed it then close
      stdin. Returns (stdout, stderr) decoded utf-8 (errors=replace).
      Raises RuntimeError on timeout."""

  def build_flat_prompt(system: str | None, messages: Sequence[LLMMessage]) -> str:
      """system + user/assistant messages joined into one flat string."""
  ```
- Consumes: `LLMMessage` (`providers/llm/base.py`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_cli_runner.py
import sys

from voice_copilot.providers.llm._cli_runner import build_flat_prompt, run_cli
from voice_copilot.providers.llm.base import LLMMessage


def test_run_cli_feeds_stdin_and_captures_stdout() -> None:
    # Echo stdin back out via a tiny python program — cross-platform.
    cmd = [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"]
    out, err = run_cli(cmd, stdin_text="hello")
    assert out == "HELLO"


def test_run_cli_without_stdin_runs_arg_mode() -> None:
    cmd = [sys.executable, "-c", "print('from-args')"]
    out, _ = run_cli(cmd)
    assert out.strip() == "from-args"


def test_run_cli_times_out() -> None:
    import pytest

    cmd = [sys.executable, "-c", "import time; time.sleep(5)"]
    with pytest.raises(RuntimeError, match="timeout"):
        run_cli(cmd, timeout=0.3)


def test_build_flat_prompt_joins_system_and_user() -> None:
    prompt = build_flat_prompt(
        "SYS", [LLMMessage(role="user", content="hi"), LLMMessage(role="assistant", content="prev")]
    )
    assert "SYS" in prompt
    assert "hi" in prompt
    assert "[assistant]: prev" in prompt
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_cli_runner.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'voice_copilot.providers.llm._cli_runner'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/voice_copilot/providers/llm/_cli_runner.py
"""Shared helpers for CLI-subprocess LLM providers (copilot-cli, auto)."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence

from voice_copilot.providers.llm.base import LLMMessage


def run_cli(cmd: list[str], *, stdin_text: str | None = None, timeout: float = 60.0) -> tuple[str, str]:
    """Run `cmd` to completion. If `stdin_text` is given, feed it then close
    stdin (signals end-of-input for interactive CLIs). Returns decoded
    (stdout, stderr). Raises RuntimeError on timeout.
    """
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if stdin_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        out, err = proc.communicate(
            input=stdin_text.encode("utf-8") if stdin_text is not None else None,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise RuntimeError(f"cli runner: timeout after {timeout}s") from None
    return (
        out.decode("utf-8", errors="replace") if out else "",
        err.decode("utf-8", errors="replace") if err else "",
    )


def build_flat_prompt(system: str | None, messages: Sequence[LLMMessage]) -> str:
    """Concatenate system + user/assistant messages into a single flat string.

    Bracket section labels are intentionally avoided by callers for CLI mode
    (they trigger file-search behaviour in some agent CLIs).
    """
    parts: list[str] = []
    if system:
        parts.append(system)
    for m in messages:
        if m.role == "user":
            parts.append(m.content)
        elif m.role == "assistant" and m.content:
            parts.append(f"[assistant]: {m.content}")
    return "\n\n".join(p.strip() for p in parts if p.strip())
```

Now refactor `copilot_cli.py` to use these. Replace its local `_build_prompt`
function and `_run_via_stdin` function and their call sites:

- Delete the `_build_prompt` function definition and the `_run_via_stdin`
  function definition.
- Add to imports: `from voice_copilot.providers.llm._cli_runner import build_flat_prompt, run_cli`.
- In `stream_chat`, change `prompt = _build_prompt(system, messages)` to
  `prompt = build_flat_prompt(system, messages)`.
- Change the `_run_via_stdin` call:
  ```python
          stdout, stderr = await loop.run_in_executor(
              None,
              lambda: run_cli(cmd, stdin_text=prompt, timeout=60.0),
          )
  ```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_cli_runner.py tests/ -v`
Expected: PASS (new runner tests + full suite; copilot_cli still imports and works)

- [ ] **Step 5: Run gates and commit**

```bash
uv run ruff check src/voice_copilot/providers/llm/_cli_runner.py src/voice_copilot/providers/llm/copilot_cli.py tests/unit/test_cli_runner.py
uv run ruff format src/voice_copilot/providers/llm/_cli_runner.py src/voice_copilot/providers/llm/copilot_cli.py
uv run mypy src/voice_copilot
git add src/voice_copilot/providers/llm/_cli_runner.py src/voice_copilot/providers/llm/copilot_cli.py tests/unit/test_cli_runner.py
git commit -m "refactor: extract shared run_cli/build_flat_prompt for CLI LLM providers"
```

---

### Task 3: Per-CLI narration-profile table

**Files:**
- Create: `src/voice_copilot/commentator/cli_profiles.py`
- Test: `tests/unit/test_commentator_cli_profiles.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class NarrationProfile:
      args: list[str]          # flags between binary and the prompt/stdin
      input_mode: str          # "stdin" | "arg"
      model: str               # cheap default model for narration

  NARRATION_PROFILES: dict[str, NarrationProfile]   # keyed by cli profile_id

  def build_narration_command(
      cli: str, binary: str, prompt: str, *, model: str | None = None
  ) -> tuple[list[str], str | None]:
      """Return (argv, stdin_text). stdin_text is the prompt for stdin-mode
      CLIs, None for arg-mode (prompt is the last argv element). Raises
      KeyError if cli has no profile."""
  ```

These flag sets are starting points verified against the real CLIs in
Task 8; a CLI that can't be made to narrate cleanly is removed from
`NARRATION_PROFILES` there (so `auto` falls back to the notice for it).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_commentator_cli_profiles.py
import pytest

from voice_copilot.commentator.cli_profiles import (
    NARRATION_PROFILES,
    build_narration_command,
)


def test_known_clis_have_profiles() -> None:
    assert set(NARRATION_PROFILES) >= {"claude", "codex", "opencode", "gemini", "copilot"}


def test_claude_command_uses_cheap_model_and_no_tools_arg_mode() -> None:
    argv, stdin_text = build_narration_command("claude", "/usr/bin/claude", "NARRATE THIS")
    assert argv[0] == "/usr/bin/claude"
    assert "--model" in argv
    assert "--allowedTools" in argv
    # arg mode → prompt is the final argv element, no stdin
    assert argv[-1] == "NARRATE THIS"
    assert stdin_text is None


def test_copilot_command_is_stdin_mode() -> None:
    argv, stdin_text = build_narration_command("copilot", "/usr/bin/copilot", "NARRATE THIS")
    assert stdin_text == "NARRATE THIS"
    assert "NARRATE THIS" not in argv


def test_model_override_is_applied() -> None:
    argv, _ = build_narration_command(
        "claude", "/usr/bin/claude", "x", model="claude-sonnet-4-6"
    )
    i = argv.index("--model")
    assert argv[i + 1] == "claude-sonnet-4-6"


def test_unknown_cli_raises() -> None:
    with pytest.raises(KeyError):
        build_narration_command("totally-unknown", "/usr/bin/x", "x")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_commentator_cli_profiles.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'voice_copilot.commentator.cli_profiles'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/voice_copilot/commentator/cli_profiles.py
"""How to invoke each supported CLI for a plain, cheap, tool-free narration
completion. Starting points; verified against the real binaries in manual
testing. A CLI that can't narrate cleanly is removed here so `auto` falls
back to the panel notice for it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NarrationProfile:
    args: list[str]
    input_mode: str  # "stdin" | "arg"
    model: str


NARRATION_PROFILES: dict[str, NarrationProfile] = {
    # copilot reads the prompt from stdin (interactive); -p triggers agent mode.
    "copilot": NarrationProfile(
        args=["--allow-all", "--no-auto-update", "-s"],
        input_mode="stdin",
        model="gpt-5-mini",
    ),
    # claude -p runs the agent; force a cheap model and disable tools so it
    # narrates instead of acting.
    "claude": NarrationProfile(
        args=["-p", "--allowedTools", ""],
        input_mode="arg",
        model="claude-haiku-4-5-20251001",
    ),
    "codex": NarrationProfile(
        args=["exec"],
        input_mode="arg",
        model="gpt-5-mini",
    ),
    "opencode": NarrationProfile(
        args=["run"],
        input_mode="arg",
        model="github-copilot/gpt-5-mini",
    ),
    "gemini": NarrationProfile(
        args=["-p"],
        input_mode="arg",
        model="gemini-2.0-flash",
    ),
}


def build_narration_command(
    cli: str, binary: str, prompt: str, *, model: str | None = None
) -> tuple[list[str], str | None]:
    """Return (argv, stdin_text) for a one-shot narration completion.

    Raises KeyError if `cli` has no narration profile.
    """
    profile = NARRATION_PROFILES[cli]
    chosen_model = model or profile.model
    argv = [binary, "--model", chosen_model, *profile.args]
    if profile.input_mode == "stdin":
        return argv, prompt
    argv.append(prompt)
    return argv, None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_commentator_cli_profiles.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Run gates and commit**

```bash
uv run ruff check src/voice_copilot/commentator/cli_profiles.py tests/unit/test_commentator_cli_profiles.py
uv run ruff format src/voice_copilot/commentator/cli_profiles.py
uv run mypy src/voice_copilot
git add src/voice_copilot/commentator/cli_profiles.py tests/unit/test_commentator_cli_profiles.py
git commit -m "feat: add per-CLI narration-profile table for the auto commentator"
```

---

### Task 4: `AutoCommentatorProvider`

**Files:**
- Create: `src/voice_copilot/providers/llm/auto.py`
- Modify: `src/voice_copilot/providers/__init__.py` and/or wherever providers are imported for side-effect registration — confirm `auto` is imported so `@register` runs (see Step 3 note)
- Test: `tests/unit/test_auto_provider.py`

**Interfaces:**
- Consumes: `build_flat_prompt`, `run_cli` (Task 2); `build_narration_command` (Task 3); `LLMProvider`, `LLMMessage` (`providers/llm/base.py`); `register` (`providers/registry.py`).
- Produces: `AutoCommentatorProvider(cli: str | None = None, binary: str | None = None, model: str | None = None)`, registered as `("llm", "auto")`, `prompt_style="cli"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_auto_provider.py
import pytest

from voice_copilot.providers.llm.auto import AutoCommentatorProvider
from voice_copilot.providers.llm.base import LLMMessage


@pytest.mark.asyncio
async def test_unset_cli_raises_clear_error() -> None:
    provider = AutoCommentatorProvider(cli=None, binary=None)
    with pytest.raises(RuntimeError, match="no launched CLI"):
        async for _ in provider.stream_chat([LLMMessage(role="user", content="hi")]):
            pass


@pytest.mark.asyncio
async def test_unsupported_cli_raises(monkeypatch) -> None:
    provider = AutoCommentatorProvider(cli="totally-unknown", binary="/usr/bin/x")
    with pytest.raises(RuntimeError, match="no narration profile"):
        async for _ in provider.stream_chat([LLMMessage(role="user", content="hi")]):
            pass


@pytest.mark.asyncio
async def test_yields_cli_output(monkeypatch) -> None:
    captured = {}

    def fake_run_cli(cmd, *, stdin_text=None, timeout=60.0):
        captured["cmd"] = cmd
        captured["stdin"] = stdin_text
        return ("we are reading the file", "")

    monkeypatch.setattr("voice_copilot.providers.llm.auto.run_cli", fake_run_cli)
    provider = AutoCommentatorProvider(cli="claude", binary="/usr/bin/claude")

    chunks = [c async for c in provider.stream_chat([LLMMessage(role="user", content="narrate")])]
    assert "".join(chunks) == "we are reading the file"
    assert captured["cmd"][0] == "/usr/bin/claude"
    assert "--model" in captured["cmd"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_auto_provider.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'voice_copilot.providers.llm.auto'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/voice_copilot/providers/llm/auto.py
"""`auto` commentator provider — generate narration by shelling out to the
same CLI the user launched via `vc`, using that CLI's own auth (no keys).

Generalises the copilot-cli pattern via a per-CLI narration-profile table.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence

from voice_copilot.commentator.cli_profiles import build_narration_command
from voice_copilot.providers.llm._cli_runner import build_flat_prompt, run_cli
from voice_copilot.providers.llm.base import LLMMessage, LLMProvider
from voice_copilot.providers.registry import register

log = logging.getLogger(__name__)


@register("llm", "auto")
class AutoCommentatorProvider(LLMProvider):
    name = "auto"
    prompt_style = "cli"

    def __init__(
        self, cli: str | None = None, binary: str | None = None, model: str | None = None
    ) -> None:
        self._cli = cli
        self._binary = binary
        self._model = model

    async def stream_chat(
        self,
        messages: Sequence[LLMMessage],
        *,
        system: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.4,
    ) -> AsyncIterator[str]:
        if not self._cli or not self._binary:
            raise RuntimeError(
                "auto commentator: no launched CLI to narrate with — run via "
                "`vc <cli>`, or pick a provider in the Commentator tab."
            )
        prompt = build_flat_prompt(system, messages)
        if not prompt:
            return
        try:
            argv, stdin_text = build_narration_command(
                self._cli, self._binary, prompt, model=self._model
            )
        except KeyError:
            raise RuntimeError(
                f"auto commentator: no narration profile for '{self._cli}' — "
                f"pick a provider in the Commentator tab."
            ) from None

        loop = asyncio.get_running_loop()
        stdout, stderr = await loop.run_in_executor(
            None, lambda: run_cli(argv, stdin_text=stdin_text, timeout=60.0)
        )
        if stderr:
            log.debug("auto(%s) stderr: %s", self._cli, stderr[:400])
        text = (stdout or "").strip()
        if not text:
            log.warning("auto(%s): empty narration response", self._cli)
            return
        yield text
```

Register `auto` for runtime `registry.build("llm", "auto", ...)`. In
`src/voice_copilot/providers/llm/__init__.py` (which already does
side-effect imports of each provider), add alongside the existing lines:

```python
from voice_copilot.providers.llm import auto as _auto  # noqa: F401
```

(The unit test imports `auto` directly so it passes regardless, but the
runtime build path needs this import to have executed `@register`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_auto_provider.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run gates and commit**

```bash
uv run ruff check src/voice_copilot/providers/llm/auto.py tests/unit/test_auto_provider.py
uv run ruff format src/voice_copilot/providers/llm/auto.py
uv run mypy src/voice_copilot
uv run pytest tests/
git add -A
git commit -m "feat: add AutoCommentatorProvider that narrates via the launched CLI"
```

---

### Task 5: Effective-provider resolution + status text

**Files:**
- Create: `src/voice_copilot/commentator/provider_select.py`
- Test: `tests/unit/test_provider_select.py`

**Interfaces:**
- Consumes: `CommentatorConfig`, `ProviderConfig` (`core/config.py`).
- Produces:
  ```python
  def resolve_commentator_provider(
      cmt: CommentatorConfig, *, cli: str | None, binary: str | None
  ) -> ProviderConfig:
      """Return the effective provider for this launch. `current` → an `auto`
      ProviderConfig carrying cli/binary/model; `api` → cmt.provider."""

  def commentator_status_text(provider: ProviderConfig, cli: str | None) -> str:
      """Human-readable 'which LLM narrates' line for the panel."""
  ```

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_provider_select.py
from voice_copilot.core.config import CommentatorConfig, CommentatorCliOverride, ProviderConfig
from voice_copilot.commentator.provider_select import (
    commentator_status_text,
    resolve_commentator_provider,
)


def test_auto_mode_resolves_to_auto_provider_with_cli() -> None:
    cmt = CommentatorConfig()  # mode defaults to "auto"
    p = resolve_commentator_provider(cmt, cli="claude", binary="/usr/bin/claude")
    assert p.name == "auto"
    assert p.options["cli"] == "claude"
    assert p.options["binary"] == "/usr/bin/claude"


def test_api_mode_resolves_to_configured_provider() -> None:
    cmt = CommentatorConfig(mode="api", provider=ProviderConfig(name="openai", options={"model": "gpt-5-mini"}))
    p = resolve_commentator_provider(cmt, cli="claude", binary="/usr/bin/claude")
    assert p.name == "openai"


def test_per_cli_override_to_api_wins_over_auto() -> None:
    cmt = CommentatorConfig(
        mode="auto",
        provider=ProviderConfig(name="openai", options={"model": "gpt-5-mini"}),
        per_cli={"gemini": CommentatorCliOverride(mode="api")},
    )
    p = resolve_commentator_provider(cmt, cli="gemini", binary="/usr/bin/gemini")
    assert p.name == "openai"


def test_per_cli_override_to_current_wins_over_api() -> None:
    cmt = CommentatorConfig(
        mode="api",
        per_cli={"claude": CommentatorCliOverride(mode="current", model="claude-sonnet-4-6")},
    )
    p = resolve_commentator_provider(cmt, cli="claude", binary="/usr/bin/claude")
    assert p.name == "auto"
    assert p.options["model"] == "claude-sonnet-4-6"


def test_status_text_mentions_cli_for_auto() -> None:
    p = ProviderConfig(name="auto", options={"cli": "claude", "binary": "/x"})
    assert "claude" in commentator_status_text(p, "claude").lower()


def test_status_text_for_auto_without_cli_is_the_pick_a_provider_fallback() -> None:
    p = ProviderConfig(name="auto", options={})
    text = commentator_status_text(p, None)
    assert "pick a provider" in text.lower()


def test_status_text_mentions_provider_for_api() -> None:
    p = ProviderConfig(name="openai", options={"model": "gpt-5-mini"})
    text = commentator_status_text(p, "claude")
    assert "openai" in text.lower()
    assert "gpt-5-mini" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_provider_select.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'voice_copilot.commentator.provider_select'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/voice_copilot/commentator/provider_select.py
"""Resolve the effective commentator provider for a `vc` launch, honoring the
global auto/api mode and optional per-CLI overrides, and describe it for the
panel status indicator.
"""

from __future__ import annotations

from voice_copilot.core.config import CommentatorConfig, ProviderConfig


def resolve_commentator_provider(
    cmt: CommentatorConfig, *, cli: str | None, binary: str | None
) -> ProviderConfig:
    override = cmt.per_cli.get(cli) if cli else None
    if override is not None:
        effective = override.mode  # "current" | "api"
        model = override.model
    else:
        effective = "current" if cmt.mode == "auto" else "api"
        model = None

    if effective == "api":
        return cmt.provider

    options: dict[str, str | int | float | bool] = {}
    if cli:
        options["cli"] = cli
    if binary:
        options["binary"] = binary
    if model:
        options["model"] = model
    return ProviderConfig(name="auto", options=options)


def commentator_status_text(provider: ProviderConfig, cli: str | None) -> str:
    if provider.name == "auto":
        target = provider.options.get("cli") or cli
        if not target:
            # auto with no launched CLI (not run via vc, or unsupported CLI)
            return (
                "Commentator: auto needs a vc-launched supported CLI — "
                "pick a provider in the Commentator tab."
            )
        model = provider.options.get("model")
        suffix = f", {model}" if model else ""
        return f"Commentator: {target} (current CLI{suffix}) — change in the Commentator tab"
    model = provider.options.get("model")
    model_part = f" {model}" if model else ""
    return f"Commentator: {provider.name}{model_part} (API) — change in the Commentator tab"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_provider_select.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Run gates and commit**

```bash
uv run ruff check src/voice_copilot/commentator/provider_select.py tests/unit/test_provider_select.py
uv run ruff format src/voice_copilot/commentator/provider_select.py
uv run mypy src/voice_copilot
git add src/voice_copilot/commentator/provider_select.py tests/unit/test_provider_select.py
git commit -m "feat: resolve effective commentator provider + status text"
```

---

### Task 6: Wire resolution + status into `vc`

**Files:**
- Modify: `src/voice_copilot/cli.py` (`_run_vc`)
- Test: `tests/unit/test_vc_commentator_wiring.py`

**Interfaces:**
- Consumes: `resolve_commentator_provider`, `commentator_status_text` (Task 5); `ResolvedCli` (already imported in cli.py).
- Produces: a small pure helper `_apply_commentator_resolution(cfg, resolved) -> str` so the wiring is unit-testable without spinning up `_run_vc`'s servers:
  ```python
  def _apply_commentator_resolution(cfg: Config, resolved: ResolvedCli | None) -> str:
      """Mutate cfg.commentator.provider to the effective provider for this
      launch and return the status text for the panel."""
  ```

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_vc_commentator_wiring.py
from voice_copilot.cli import _apply_commentator_resolution
from voice_copilot.core.config import Config
from voice_copilot.proxy.cli_shims import ResolvedCli


def test_known_cli_sets_auto_provider_and_status() -> None:
    cfg = Config()  # commentator.mode defaults to auto
    resolved = ResolvedCli(
        profile_id="claude",
        label="Claude Code",
        resolved_binary="/usr/bin/claude",
        env_overrides={"ANTHROPIC_BASE_URL": "http://x"},
        working_directory=None,
    )
    status = _apply_commentator_resolution(cfg, resolved)
    assert cfg.commentator.provider.name == "auto"
    assert cfg.commentator.provider.options["cli"] == "claude"
    assert "claude" in status.lower()


def test_unknown_cli_leaves_auto_without_cli() -> None:
    cfg = Config()
    status = _apply_commentator_resolution(cfg, None)
    # mode=auto + no cli → auto provider with no cli (will fail loud at narrate)
    assert cfg.commentator.provider.name == "auto"
    assert "cli" not in cfg.commentator.provider.options
    assert "Commentator" in status
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_vc_commentator_wiring.py -v`
Expected: FAIL with `ImportError: cannot import name '_apply_commentator_resolution'`

- [ ] **Step 3: Write minimal implementation**

In `src/voice_copilot/cli.py`, add the imports:

```python
from voice_copilot.commentator.provider_select import (
    commentator_status_text,
    resolve_commentator_provider,
)
```

Add the helper (place it near `_run_vc`, before it):

```python
def _apply_commentator_resolution(cfg: Config, resolved: ResolvedCli | None) -> str:
    """Set cfg.commentator.provider to the effective provider for this launch
    and return the panel status text."""
    cli = resolved.profile_id if resolved is not None else None
    binary = resolved.resolved_binary if resolved is not None else None
    effective = resolve_commentator_provider(cfg.commentator, cli=cli, binary=binary)
    cfg.commentator.provider = effective
    return commentator_status_text(effective, cli)
```

In `_run_vc`, after `cfg` is available from `_boot` and **before** the
`commentator = Commentator(bus, cfg.commentator, ...)` line, add:

```python
    commentator_status = _apply_commentator_resolution(cfg, resolved)
```

Then, where `_run_vc` currently sets the launch notice (the
`_server_app_state(server).launch_notice = ...` lines for the resolved /
unknown branches added during the vc terminal work), replace BOTH branch
assignments with a single line after them that appends the commentator
status, so the banner shows narration status too. Concretely, set:

```python
    _server_app_state(server).launch_notice = commentator_status
```

(If a CLI-not-recognized notice is also desired, join them:
`f"{recognized_note}  •  {commentator_status}"` — keep it one string.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_vc_commentator_wiring.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Run gates + smoke and commit**

```bash
uv run ruff check src/voice_copilot/cli.py tests/unit/test_vc_commentator_wiring.py
uv run ruff format src/voice_copilot/cli.py
uv run mypy src/voice_copilot
uv run pytest tests/
uv run voice-copilot version
git add src/voice_copilot/cli.py tests/unit/test_vc_commentator_wiring.py
git commit -m "feat: wire commentator auto-resolution + status banner into vc"
```

---

### Task 7: Settings UI — Commentator tab (mode toggle + per-CLI + status)

**Files:**
- Modify: `src/voice_copilot/web/static/index.html` (Commentator tab)
- Modify: `src/voice_copilot/web/static/app.js` (per_cli render/save; mode toggle uses the existing dotted-name walker for `commentator.mode`)

No automated frontend tests exist (consistent with the project); verified
in Task 8. Keep edits surgical and match existing patterns.

- [ ] **Step 1: Read the current Commentator tab + config walker**

Read `src/voice_copilot/web/static/index.html` (the `data-panel="llm"`
section) and `app.js`'s `loadConfig`/`saveConfig` (the `form.elements`
dotted-name walker) to find exact insertion points and the existing
provider/model fields.

- [ ] **Step 2: Add the global mode toggle**

In the Commentator panel in `index.html`, add a select bound to
`commentator.mode` (works with the existing walker — no JS needed):

```html
        <label>Commentator mode
          <select name="commentator.mode">
            <option value="auto">Auto — use the CLI I launched (no keys)</option>
            <option value="api">API — use the provider below</option>
          </select>
        </label>
```

- [ ] **Step 3: Add the per-CLI overrides table + render/save JS**

In `index.html`, add a container inside the Commentator panel:

```html
        <h3 class="section-title">Per-CLI overrides (optional)</h3>
        <div id="commentator-per-cli"></div>
```

In `app.js`, after `loadConfig()` populates the form, render the per-CLI
rows from `cfg.commentator.per_cli`, and in `saveConfig()` read them back
into `cfg.commentator.per_cli`. Add a small renderer that lists the five
known CLIs (`claude`, `codex`, `opencode`, `gemini`, `copilot`), each with a
mode `<select>` (default / current / api) and a model `<input>`; "default"
means absent from `per_cli`. Use the established `qs`/`qsa` helpers and the
existing fetch-`/api/config` + POST pattern. Keep it self-contained in one
`renderPerCli(cfg)` + `collectPerCli()` pair wired into the existing
`loadConfig`/`saveConfig`.

- [ ] **Step 4: Verify JS validity + Python suite unaffected**

```bash
node --check src/voice_copilot/web/static/app.js
uv run pytest tests/
```
Expected: app.js valid; 54+ tests still pass (no Python touched).

- [ ] **Step 5: Commit**

```bash
git add src/voice_copilot/web/static/index.html src/voice_copilot/web/static/app.js
git commit -m "feat: Commentator tab — auto/api mode, per-CLI overrides, status"
```

---

### Task 8: Manual verification — the empirical per-CLI gate

No automated test can confirm each real CLI narrates cleanly. Per
verification-before-completion, do not consider this plan done until this
passes. This is also where `cli_profiles.py` flag sets get tuned.

- [ ] **Step 1: claude (primary)**

`uv run voice-copilot vc claude`, ask it something, and confirm narration
appears in the Trace, is reasonably fast, uses the cheap model, and reads
like narration (not the agent doing work). If `claude -p --allowedTools ""`
misbehaves (acts/too slow/empty), adjust the `claude` profile in
`commentator/cli_profiles.py` (e.g. different flags, stdin mode, model) and
re-test. Check `~/.voice-copilot/vc-session.log` for the actual command and
any errors.

- [ ] **Step 2: codex / opencode / gemini / copilot**

Repeat for each you have installed. Tune each profile until narration is
clean, or remove that CLI from `NARRATION_PROFILES` (so `auto` shows the
fallback notice for it) if it can't be made to work for v1.

- [ ] **Step 3: API mode + per-CLI override**

In the Commentator tab, switch mode to API with a working provider; confirm
narration uses it and the status banner updates. Set a per-CLI override and
confirm it wins for that CLI.

- [ ] **Step 4: Fallback path (unknown CLI via vc)**

Run `uv run voice-copilot vc cmd` (an unrecognized CLI). Confirm the panel
status banner shows the "auto needs a vc-launched supported CLI — pick a
provider in the Commentator tab" message (resolved=None → `auto` with no
cli), rather than silently failing. (Note: `serve`/`proxy` keep the existing
configured-provider behaviour — auto-resolution is wired only into `vc` for
v1, per the spec's non-goal.)

- [ ] **Step 5: Commit any profile tuning + record results**

```bash
git add src/voice_copilot/commentator/cli_profiles.py
git commit -m "fix: tune per-CLI narration profiles from manual verification"
```
Record pass/fail per CLI in the PR/commit description; do not mark the plan
complete with a profile that narrates incorrectly still enabled.
