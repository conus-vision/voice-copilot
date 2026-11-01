# ruff: noqa: RUF001
"""The `vc ...` words of the chat: exactly what counts as one, and what they say.

A CLI's user talks to an agent in the same box, and may well talk about version
control or about this tool. So a message is a command only if all of it fits the
grammar below, on one line. "vc off" is a command; "vc off please", "vc off the
build" and "what does vc off do?" go to the agent untouched. When in doubt it is
not a command: the worst case is one model call, where the opposite would swallow
what the user meant to ask.

Imported by the hook process, which runs on every tool call: standard library only.
"""

from __future__ import annotations

import re
from typing import NamedTuple


class Command(NamedTuple):
    verb: str
    args: tuple[str, ...] = ()


#: Every spelling of a verb. Russian ones are there for whoever thinks in it.
_VERBS = {
    "help": "help",
    "помощь": "help",
    "on": "on",
    "вкл": "on",
    "off": "off",
    "выкл": "off",
    "status": "status",
    "статус": "status",
    "check": "check",
    "проверь": "check",
    "speed": "speed",
    "скорость": "speed",
    "volume": "volume",
    "громкость": "volume",
    "mute": "mute",
    "тихо": "mute",
    "unmute": "unmute",
    "звук": "unmute",
    "pause": "pause",
    "пауза": "pause",
    "go": "go",
    "дальше": "go",
    "model": "model",
    "модель": "model",
    "review": "review",
    "анализ": "review",
    "detail": "detail",
    "детали": "detail",
    "access": "access",
    "доступ": "access",
    "settings": "settings",
    "настройки": "settings",
    "log": "log",
    "журнал": "log",
    "cost": "cost",
    "расход": "cost",
    "ask": "ask",
    "спроси": "ask",
}

#: Words a shell's `vc <word>` takes for one of these rather than for a CLI to launch.
WORDS = frozenset(_VERBS)

_ALL = {"all": "all", "все": "all"}
_VALUES = {
    "review": {
        "off": "off",
        "выкл": "off",
        "watch": "watch",
        "смотреть": "watch",
        "guard": "guard",
        "стоп": "guard",
    },
    "detail": {
        "low": "low",
        "мало": "low",
        "normal": "normal",
        "обычно": "normal",
        "high": "high",
        "много": "high",
    },
    "access": {
        "events": "events",
        "события": "events",
        "edits": "edits",
        "правки": "edits",
        "files": "files",
        "файлы": "files",
    },
}
_NO_ARGS = {"help", "status", "check", "mute", "unmute", "pause", "go", "settings", "log", "cost"}
_SPEED = re.compile(r"\d+(?:[.,]\d+)?x?", re.IGNORECASE)
_VOLUME = re.compile(r"\d{1,3}%?")
#: A number from the list, or a model's name, which has no spaces in it.
_MODEL = re.compile(r"[\w.:/@+-]+")
_MAX_CHARS = 2000


def _fits(verb: str, args: list[str]) -> Command | None:
    if verb in _NO_ARGS:
        return None if args else Command(verb)
    if verb in ("on", "off"):
        if not args:
            return Command(verb)
        scope = _ALL.get(args[0].lower())
        return Command(verb, (scope,)) if scope and len(args) == 1 else None
    if verb == "speed":
        return _one_of(verb, args, _SPEED)
    if verb == "volume":
        return _one_of(verb, args, _VOLUME)
    if verb == "model":
        return _one_of(verb, args, _MODEL)
    if verb == "ask":
        return Command(verb, (" ".join(args),)) if args else None
    if verb == "review" and args and args[0].lower() in ("model", "модель"):
        rest = _one_of(verb, args[1:], _MODEL)
        return None if rest is None else Command(verb, ("model", *rest.args))
    values = _VALUES.get(verb)
    if values is None or len(args) > 1:
        return None
    if not args:
        return Command(verb)
    value = values.get(args[0].lower())
    return Command(verb, (value,)) if value else None


def _one_of(verb: str, args: list[str], shape: re.Pattern[str]) -> Command | None:
    if not args:
        return Command(verb)
    if len(args) == 1 and shape.fullmatch(args[0]):
        return Command(verb, (args[0],))
    return None


def parse(text: str) -> Command | None:
    """The command this whole message is, or None if it is for the agent."""
    # Cheap on purpose: this sees every prompt. `.strip()` drops the newline a paste can end in.
    message = text.strip()
    if len(message) > _MAX_CHARS or "\n" in message or message[:2].lower() != "vc":
        return None
    words = message.split()
    if words[0].lower() != "vc":
        return None
    if len(words) == 1:
        return Command("help")
    verb = _VERBS.get(words[1].lower())
    return None if verb is None else _fits(verb, words[2:])


_TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "menu": (
            "Voice Copilot. Type any of these as the whole message:\n"
            "  vc on | vc off          this session; add `all` for every session\n"
            "  vc status               how it stands, and where the panel is\n"
            "  vc check                have it look at the agent right now\n"
            "  vc ask <question>       ask the second model, without the agent\n"
            "  vc log | vc cost        what it said; what it has spent\n"
            "  vc speed 1.5            speech speed\n"
            "  vc volume 70            volume; vc mute, vc unmute\n"
            "  vc model                the narrator's model: lists them, vc model 2 picks\n"
            "  vc review off|watch|guard   the supervisor; vc review model picks its model\n"
            "  vc detail low|normal|high   how much the narrator says\n"
            "  vc access events|edits|files   what the models may read\n"
            "  vc pause | vc go        stop the agent before its next action; carry on\n"
            "  vc settings             open the settings window"
        ),
        "off_here": "Voice Copilot is off for this session. `vc on` brings it back, `vc off all` stops every session.",
        "off_all": "Voice Copilot is off in every session, new ones included. `vc on all` brings it back.",
        "on_here": "Voice Copilot is on for this session. Panel: {url}",
        "on_all": "Voice Copilot is on. Panel: {url}",
        "on_no_panel": "Voice Copilot is on, but its companion didn't start. Run `voice-copilot companion`.",
        "status_off": "Voice Copilot is off. `vc on` brings it back.",
        "status_on": "Voice Copilot is on. Panel: {url}",
        "status_waiting": "Voice Copilot is on. Its companion starts with the next session, or with `vc on`.",
        "no_companion": "Voice Copilot's companion isn't running. `vc on` starts it.",
        "failed": "Voice Copilot's companion didn't take that: {error}",
        "connected": "Voice Copilot is connected. For the list of commands, type vc",
    },
    "ru": {
        "menu": (
            "Voice Copilot. Введите одну из команд отдельным сообщением:\n"
            "  vc on | vc off          эта сессия; с `all` все сессии\n"
            "  vc status               состояние и адрес панели\n"
            "  vc check                посмотреть на агента прямо сейчас\n"
            "  vc ask <вопрос>         спросить вторую модель, без агента\n"
            "  vc log | vc cost        что говорил; сколько потрачено\n"
            "  vc speed 1.5            скорость речи\n"
            "  vc volume 70            громкость; vc mute, vc unmute\n"
            "  vc model                модель рассказчика: список, vc model 2 выбирает\n"
            "  vc review off|watch|guard   супервизор; vc review model выбирает его модель\n"
            "  vc detail low|normal|high   сколько говорит рассказчик\n"
            "  vc access events|edits|files   что могут читать модели\n"
            "  vc pause | vc go        остановить агента перед следующим действием; продолжить\n"
            "  vc settings             открыть окно настроек"
        ),
        "off_here": "Voice Copilot выключен для этой сессии. Включить: `vc on`, выключить все сессии: `vc off all`.",
        "off_all": "Voice Copilot выключен во всех сессиях, включая новые. Включить: `vc on all`.",
        "on_here": "Voice Copilot включён для этой сессии. Панель: {url}",
        "on_all": "Voice Copilot включён. Панель: {url}",
        "on_no_panel": "Voice Copilot включён, но компаньон не запустился. Выполните `voice-copilot companion`.",
        "status_off": "Voice Copilot выключен. Включить: `vc on`.",
        "status_on": "Voice Copilot включён. Панель: {url}",
        "status_waiting": "Voice Copilot включён. Компаньон запустится со следующей сессией или по `vc on`.",
        "no_companion": "Компаньон Voice Copilot не запущен. Запустить: `vc on`.",
        "failed": "Компаньон Voice Copilot не принял команду: {error}",
        "connected": "Voice Copilot подключён. Список команд: vc",
    },
}


def say(key: str, language: str = "en", **fields: object) -> str:
    table = _TEXTS.get(language, _TEXTS["en"])
    return table.get(key, _TEXTS["en"][key]).format(**fields)
