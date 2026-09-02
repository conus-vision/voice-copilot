"""Runtime configuration. YAML at `~/.voice-copilot/config.yaml` + env overrides."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

import yaml
from platformdirs import user_config_path
from pydantic import BaseModel, Field

from voice_copilot.proxy.cli_catalog import CLI_CATALOG

Language = Literal["en", "es", "fr", "uk", "ru"]


class ProviderConfig(BaseModel):
    name: str
    options: dict[str, str | int | float | bool] = Field(default_factory=dict)


class HotkeysConfig(BaseModel):
    push_to_talk: str = "alt+space"
    interrupt: str = "alt+shift+space"
    mute_toggle: str = "alt+m"
    pause_toggle: str = "alt+p"
    skip_current: str = "alt+shift+n"


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765
    open_browser: bool = True


ProxyRoute = Literal[
    "anthropic",
    "openai",
    "openai-chatgpt",
    "openrouter",
    "groq",
    "mistral",
    "deepseek",
    "ollama",
    "gemini",
    "opencode-zen",
]


class ProxyCliProfileConfig(BaseModel):
    provider: ProxyRoute
    base_url_env: str
    binary_path: str | None = None
    # Legacy per-profile override. Prefer `proxy_cli.working_directory`.
    working_directory: str | None = None


def default_proxy_cli_profiles() -> dict[str, ProxyCliProfileConfig]:
    return {
        profile_id: ProxyCliProfileConfig(
            provider=cast(ProxyRoute, meta.provider),
            base_url_env=meta.base_url_env,
        )
        for profile_id, meta in CLI_CATALOG.items()
    }


class ProxyCliConfig(BaseModel):
    working_directory: str | None = None
    profiles: dict[str, ProxyCliProfileConfig] = Field(default_factory=default_proxy_cli_profiles)


class DialogConfig(BaseModel):
    """Routing rules for user voice messages into the running CLI."""

    hold_agent_while_narrating: bool = False
    """Suspend the child process for as long as a narration line is read out.

    Off by default: it trades throughput for comprehension, so it stays the
    user's call. Toggled live from the panel — the agent works in bursts
    between sentences instead of running ahead while you listen.
    """

    auto_pause_on_speak: bool = False
    """Suspend the child process while the user holds push-to-talk.

    Off by default — pausing mid-tool-call is safe (just threads frozen) but
    can be surprising if a long-running command is in flight. Users who want
    undivided attention can flip this on.
    """

    deliver_immediately: bool = True
    """If True, push USER_MESSAGE into the adapter as soon as it arrives.
    If False, buffer until TURN_ENDED (useful if the CLI reacts poorly to
    mid-turn stdin writes).
    """


class VoiceInputConfig(BaseModel):
    """Push-to-talk → STT → inject-into-the-CLI chain.

    Disabled by default while the voice-control flow is being reworked: the
    panel ships listen-only (narration out, no mic in). Set
    `voice_input.enabled: true` in the config to bring the microphone, the
    push-to-talk hotkey and the Speech-input settings back.
    """

    enabled: bool = False


class FocusConfig(BaseModel):
    narrate_only_when_focused: bool = True


class CommentatorCliOverride(BaseModel):
    """Per-CLI knobs for narrator and supervisor; "default" defers to the
    global setting so a row can override one without touching the other."""

    mode: Literal["default", "current", "api"] = "default"
    model: str | None = None
    supervisor_mode: Literal["default", "off", "watch", "guard"] = "default"
    supervisor_model: str | None = None


class SupervisorConfig(BaseModel):
    """A stronger model that reviews the agent at checkpoints.

    The narrator is cheap and talks every few seconds; the supervisor is
    expensive and speaks only when a checkpoint arrives — the end of a turn, a
    run of tool calls, a failure that repeats — and only if something is off.
    """

    #: off — never runs. watch — says so out loud when the agent drifts.
    #: guard — additionally pauses the agent and waits for you.
    mode: Literal["off", "watch", "guard"] = "off"
    #: Model for the supervisor call; empty picks the CLI's strong default
    #: (auto) or the API provider's configured model.
    model: str | None = None
    #: Review after this many tool calls even if the turn hasn't ended.
    every_n_tools: int = 8


class CommentatorConfig(BaseModel):
    provider: ProviderConfig = ProviderConfig(
        name="anthropic", options={"model": "claude-haiku-4-5-20251001"}
    )
    mode: Literal["auto", "api"] = "auto"
    per_cli: dict[str, CommentatorCliOverride] = Field(default_factory=dict)
    debounce_ms: int = 1200
    #: Speak up after this much silence even when only quiet activity has
    #: piled up (0 disables). A long stretch of greps, reads and shell runs
    #: carries no thinking or answer, so without this the narrator would
    #: wait it out in silence.
    idle_narration_ms: int = 15000
    min_importance: Literal["low", "normal", "high"] = "normal"
    speak_tool_calls: bool = True
    speak_thinking: bool = True
    speak_file_edits: bool = True
    supervisor: SupervisorConfig = Field(default_factory=SupervisorConfig)
    #: Pick the models automatically: the weakest the CLI offers for the
    #: narrator, the strongest for the supervisor. Read from the CLI's own
    #: catalog where there is one (codex), static tiers otherwise. An
    #: explicit per-CLI model still wins.
    auto_tier_models: bool = False


class Config(BaseModel):
    human_language: Language = "en"
    commentator_language: Language = "en"
    server: ServerConfig = ServerConfig()
    hotkeys: HotkeysConfig = HotkeysConfig()
    tts: ProviderConfig = ProviderConfig(name="edge-tts")
    stt: ProviderConfig = ProviderConfig(name="openai-whisper-api")
    commentator: CommentatorConfig = CommentatorConfig()
    dialog: DialogConfig = DialogConfig()
    voice_input: VoiceInputConfig = VoiceInputConfig()
    focus: FocusConfig = FocusConfig()
    proxy_cli: ProxyCliConfig = ProxyCliConfig()


def _normalize_config(cfg: Config) -> Config:
    for profile_id, default_profile in default_proxy_cli_profiles().items():
        cfg.proxy_cli.profiles.setdefault(profile_id, default_profile)

    if not cfg.proxy_cli.working_directory:
        for profile in cfg.proxy_cli.profiles.values():
            if profile.working_directory:
                cfg.proxy_cli.working_directory = profile.working_directory
                break

    opencode_profile = cfg.proxy_cli.profiles.get("opencode")
    if opencode_profile is not None and opencode_profile.provider == "opencode-zen":
        opencode_profile.base_url_env = "OPENCODE_CONFIG_CONTENT"

    # Configs written before the ChatGPT route existed pin codex to `openai`,
    # which points at api.openai.com — a host that rejects the ChatGPT OAuth
    # bearer a plan login sends, so those sessions were never narrated. Move
    # them onto the route that matches how codex actually authenticates; a user
    # running codex on an API key can pick `openai` again in the panel.
    codex_profile = cfg.proxy_cli.profiles.get("codex")
    if codex_profile is not None and codex_profile.provider == "openai":
        codex_profile.provider = "openai-chatgpt"

    return cfg


def config_path() -> Path:
    return user_config_path("voice-copilot", appauthor=False) / "config.yaml"


def proxy_cli_config_path(config_file: Path | None = None) -> Path:
    base_dir = (
        config_file.parent
        if config_file is not None
        else user_config_path("voice-copilot", appauthor=False)
    )
    return base_dir / "proxy-cli.yaml"


def _load_yaml_doc(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return raw if isinstance(raw, dict) else {}


def _migrate_legacy_language(doc: dict[str, Any]) -> dict[str, Any]:
    legacy_language = doc.pop("language", None)
    if isinstance(legacy_language, str):
        doc.setdefault("human_language", legacy_language)
        doc.setdefault("commentator_language", legacy_language)
    return doc


def load_config(path: Path | None = None) -> Config:
    p = path or config_path()
    main_doc = _migrate_legacy_language(_load_yaml_doc(p))
    embedded_proxy_doc = main_doc.pop("proxy_cli", None)
    cfg = Config.model_validate(main_doc or {})

    proxy_doc = _load_yaml_doc(proxy_cli_config_path(p))
    if not proxy_doc and isinstance(embedded_proxy_doc, dict):
        proxy_doc = embedded_proxy_doc
    if proxy_doc:
        cfg.proxy_cli = ProxyCliConfig.model_validate(proxy_doc)

    return _normalize_config(cfg)


def save_config(cfg: Config, path: Path | None = None) -> None:
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    proxy_path = proxy_cli_config_path(p)
    p.write_text(
        yaml.safe_dump(cfg.model_dump(exclude={"proxy_cli"}), sort_keys=False),
        encoding="utf-8",
    )
    proxy_path.write_text(
        yaml.safe_dump(cfg.proxy_cli.model_dump(), sort_keys=False),
        encoding="utf-8",
    )
