"""Runtime configuration. YAML at `~/.voice-copilot/config.yaml` + env overrides."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from platformdirs import user_config_path
from pydantic import BaseModel, Field

Language = Literal["en", "es", "fr", "uk", "ru"]


class ProviderConfig(BaseModel):
    name: str
    options: dict[str, str | int | float | bool] = Field(default_factory=dict)


class HotkeysConfig(BaseModel):
    push_to_talk: str = "alt+space"
    interrupt: str = "alt+shift+space"
    mute_toggle: str = "alt+m"
    pause_toggle: str = "alt+p"


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765
    open_browser: bool = True


class DialogConfig(BaseModel):
    """Routing rules for user voice messages into the running CLI."""

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


class CommentatorConfig(BaseModel):
    provider: ProviderConfig = ProviderConfig(name="anthropic", options={"model": "claude-haiku-4-5-20251001"})
    debounce_ms: int = 1200
    min_importance: Literal["low", "normal", "high"] = "normal"
    speak_tool_calls: bool = True
    speak_thinking: bool = True
    speak_file_edits: bool = True


class Config(BaseModel):
    language: Language = "en"
    server: ServerConfig = ServerConfig()
    hotkeys: HotkeysConfig = HotkeysConfig()
    tts: ProviderConfig = ProviderConfig(name="edge-tts")
    stt: ProviderConfig = ProviderConfig(name="openai-whisper-api")
    commentator: CommentatorConfig = CommentatorConfig()
    dialog: DialogConfig = DialogConfig()


def config_path() -> Path:
    return user_config_path("voice-copilot", appauthor=False) / "config.yaml"


def load_config(path: Path | None = None) -> Config:
    p = path or config_path()
    if not p.exists():
        return Config()
    return Config.model_validate(yaml.safe_load(p.read_text(encoding="utf-8")) or {})


def save_config(cfg: Config, path: Path | None = None) -> None:
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(cfg.model_dump(), sort_keys=False), encoding="utf-8")
