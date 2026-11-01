# ruff: noqa: RUF001
"""What the `vc` words do once they reach a running Voice Copilot.

`commands.py` decides what counts as a command, and the hook process or the proxy carries
it here. Every CLI gets the same set this way, whichever route it came by. Answers go back
as `{"text": ..., "choices": [...]}`: `choices` is for a plugin whose CLI can draw a picker,
and the text always stands on its own for one that can't.
"""

from __future__ import annotations

import asyncio
import logging
import webbrowser
from collections import Counter
from collections.abc import Callable
from typing import Any

from voice_copilot.adapters.base import CLIAdapter
from voice_copilot.audio.hub import AudioHub
from voice_copilot.commentator.model_catalog import listed_models, ollama_models
from voice_copilot.commentator.pipeline import Commentator
from voice_copilot.core.bus import EventBus
from voice_copilot.core.config import CommentatorCliOverride, Config, ProviderConfig
from voice_copilot.core.events import Event, EventKind
from voice_copilot.core.secrets import get_secret
from voice_copilot.web.server import ConfigRejected, apply_config

log = logging.getLogger(__name__)

_SPEED_RANGE = (0.5, 3.0)
#: Roughly how many characters make a token, for `vc cost`, which has only the text to go on.
_CHARS_PER_TOKEN = 4
_LOG_LINES = 8
#: A chat isn't a place for a catalog of hundreds. The rest can still be picked by name.
_LISTED_MODELS = 30

#: `vc detail high` means more talk, which is a lower bar for what is worth saying.
_DETAIL_TO_IMPORTANCE = {"low": "high", "normal": "normal", "high": "low"}
_IMPORTANCE_TO_DETAIL = {v: k for k, v in _DETAIL_TO_IMPORTANCE.items()}

#: Who needs a key, for models named `<provider>/<model>`. Ollama needs none.
_KEYS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}

_TEXTS: dict[str, dict[str, Any]] = {
    "en": {
        "speed_now": "Speech speed: {value}. Change it with vc speed 1.5, from {low} to {high}.",
        "speed_set": "Speech speed: {value}.{no_panel}",
        "speed_range": "Speed goes from {low} to {high}.",
        "volume_now": "Volume: {value}%. Change it with vc volume 70.",
        "volume_set": "Volume: {value}%.{no_panel}",
        "volume_range": "Volume goes from 0 to 100.",
        "muted": "Voice muted. The agent carries on. vc unmute brings it back.{no_panel}",
        "unmuted": "Voice back on.{no_panel}",
        "no_panel": " The panel isn't open, so this applies once it is: vc settings.",
        "paused": (
            "The agent stops before its next action. vc go resumes it. "
            "If that doesn't get through, use Alt+P or the button in the panel."
        ),
        "paused_proxy": "The agent is frozen. Resume with Alt+P or the button in the panel.",
        "paused_already": "The agent is already paused. vc go resumes it.",
        "resumed": "The agent carries on.",
        "not_paused": "The agent isn't paused.",
        "model_now": "The narrator's model: {current}. Pick with vc model <number or name>:\n{listing}",
        "review_model_now": (
            "The supervisor's model: {current}. Pick with vc review model <number or name>:\n{listing}"
        ),
        "no_list": "This CLI gives no list of its models. Name one: vc model <name>.",
        "model_set": "The narrator now uses {name}.",
        "review_model_set": "The supervisor now uses {name}.",
        "no_such_number": "There is no number {value} on the list. vc model shows it.",
        "needs_key": (
            "{name} needs an API key. Keys go in the settings window only, never in the chat: "
            "the chat keeps a copy. Opening it: {url}"
        ),
        "no_cli": "No CLI has reported in yet, so name a model with its provider, such as ollama/gemma4:12b.",
        "review_now": "The supervisor: {value}. Change it with vc review off, watch or guard.",
        "review_set": {
            "off": "The supervisor is off. The narrator carries on.",
            "watch": "The supervisor reviews the agent and speaks up when something is off.",
            "guard": "The supervisor reviews the agent, and stops it when something is wrong.",
        },
        "detail_now": "Detail: {value}. Change it with vc detail low, normal or high.",
        "detail_set": "Detail: {value}.",
        "access_now": "File access: {value}. Change it with vc access events, edits or files.",
        "access_set": {
            "events": "The models see only events: tools, commands, their output.",
            "edits": "The supervisor also sees the latest edits, up to six, in part.",
            "files": (
                "The supervisor also reads the files the agent changed, the latest three, whole. "
                "Their text goes to {model}. Files that look like secrets are skipped."
            ),
        },
        "settings": "Settings window: {url}",
        "log_empty": "Nothing said yet.",
        "cost": (
            "So far: {narrator} narrator calls, {summary} summaries, {supervisor} reviews, "
            "{question} questions. About {tokens_in}k tokens of prompts and {tokens_out}k of "
            "answers, from the text alone: a CLI adds its own prompt on top of each call."
        ),
        "asking": "Asking {model}. You'll hear the answer, and see it here.",
        "asking_proxy": "Asking {model}. You'll hear the answer, and see it in the panel.",
        "check": "Having a look at the agent. Listen for the answer, or see the panel.",
        "no_session": "No session is attached yet, so there is nothing to look at.",
        "answer_failed": "Couldn't get an answer: {error}",
        "warned": "Voice Copilot's supervisor: {message}",
        "test_line": "Voice Copilot is connected. This is how it sounds.",
        "tested": "You should hear a test line now. If not, click in the panel's tab once.",
        "unknown": "Voice Copilot doesn't know that command. For the list, type vc",
    },
    "ru": {
        "speed_now": "Скорость речи: {value}. Изменить: vc speed 1.5, от {low} до {high}.",
        "speed_set": "Скорость речи: {value}.{no_panel}",
        "speed_range": "Скорость от {low} до {high}.",
        "volume_now": "Громкость: {value}%. Изменить: vc volume 70.",
        "volume_set": "Громкость: {value}%.{no_panel}",
        "volume_range": "Громкость от 0 до 100.",
        "muted": "Голос выключен, агент продолжает работу. Вернуть: vc unmute.{no_panel}",
        "unmuted": "Голос включён.{no_panel}",
        "no_panel": " Панель закрыта, поэтому это применится, когда она откроется: vc settings.",
        "paused": (
            "Агент остановится перед следующим действием. Продолжить: vc go. "
            "Если не сработает: Alt+P или кнопка в панели."
        ),
        "paused_proxy": "Агент заморожен. Продолжить: Alt+P или кнопка в панели.",
        "paused_already": "Агент уже на паузе. Продолжить: vc go.",
        "resumed": "Агент продолжает.",
        "not_paused": "Агент не на паузе.",
        "model_now": "Модель рассказчика: {current}. Выбрать: vc model <номер или имя>:\n{listing}",
        "review_model_now": (
            "Модель супервизора: {current}. Выбрать: vc review model <номер или имя>:\n{listing}"
        ),
        "no_list": "Этот CLI не отдаёт список моделей. Укажите имя: vc model <имя>.",
        "model_set": "Рассказчик теперь использует {name}.",
        "review_model_set": "Супервизор теперь использует {name}.",
        "no_such_number": "В списке нет номера {value}. Список: vc model.",
        "needs_key": (
            "Для {name} нужен ключ API. Ключи вводятся только в окне настроек, не в чате: "
            "чат хранит копию. Открываю: {url}"
        ),
        "no_cli": "Ни один CLI ещё не подключился, поэтому укажите модель с провайдером, например ollama/gemma4:12b.",
        "review_now": "Супервизор: {value}. Изменить: vc review off, watch или guard.",
        "review_set": {
            "off": "Супервизор выключен. Рассказчик продолжает.",
            "watch": "Супервизор проверяет агента и говорит, если что-то не так.",
            "guard": "Супервизор проверяет агента и останавливает его, если что-то не так.",
        },
        "detail_now": "Подробность: {value}. Изменить: vc detail low, normal или high.",
        "detail_set": "Подробность: {value}.",
        "access_now": "Доступ к файлам: {value}. Изменить: vc access events, edits или files.",
        "access_set": {
            "events": "Модели видят только события: инструменты, команды, их вывод.",
            "edits": "Супервизор видит ещё и последние правки, до шести, частично.",
            "files": (
                "Супервизор читает целиком ещё и изменённые агентом файлы, три последних. "
                "Их текст уходит модели {model}. Файлы, похожие на секреты, пропускаются."
            ),
        },
        "settings": "Окно настроек: {url}",
        "log_empty": "Пока ничего не сказано.",
        "cost": (
            "Пока: вызовов рассказчика {narrator}, сводок {summary}, проверок {supervisor}, "
            "вопросов {question}. Примерно {tokens_in} тыс. токенов запросов и {tokens_out} тыс. "
            "ответов, по одному только тексту: CLI добавляет к каждому вызову свой промпт."
        ),
        "asking": "Спрашиваю {model}. Ответ прозвучит и появится здесь.",
        "asking_proxy": "Спрашиваю {model}. Ответ прозвучит и появится в панели.",
        "check": "Смотрю на агента. Слушайте ответ или смотрите панель.",
        "no_session": "К сессии ещё ничего не подключено, смотреть не на что.",
        "answer_failed": "Не удалось получить ответ: {error}",
        "warned": "Супервизор Voice Copilot: {message}",
        "test_line": "Voice Copilot подключён. Так звучит его голос.",
        "tested": "Сейчас должна прозвучать пробная фраза. Если нет, кликните один раз во вкладке панели.",
        "unknown": "Voice Copilot не знает такой команды. Список: vc",
    },
}


class CommandCenter:
    def __init__(
        self,
        *,
        bus: EventBus,
        state: Any,
        commentator: Commentator,
        adapter: CLIAdapter,
        hub: AudioHub,
        panel_url: str,
        active_cli: Callable[[], str | None],
        active_session: Callable[[], str | None] = lambda: None,
        chat_replies: bool = True,
    ) -> None:
        self._bus = bus
        self._state = state
        self._commentator = commentator
        self._adapter = adapter
        self._hub = hub
        self._panel_url = panel_url.rstrip("/")
        self._active_cli = active_cli
        self._active_session = active_session
        #: Whether a late answer can be shown in the CLI, not just heard and shown in the panel.
        self._chat_replies = chat_replies
        self._answers: set[asyncio.Task[None]] = set()
        #: What the panel last reported, and what it should apply once it opens.
        self._panel: dict[str, Any] = {"playback_rate": 1.2, "volume": 100, "muted": False}
        self._state.panel_overrides = {}

    @property
    def _language(self) -> str:
        return str(self._state.config.commentator_language)

    def _say(self, key: str, **fields: object) -> str:
        table = _TEXTS.get(self._language, _TEXTS["en"])
        text = table.get(key, _TEXTS["en"][key])
        return text.format(**fields) if isinstance(text, str) else str(text)

    def _say_choice(self, key: str, choice: str, **fields: object) -> str:
        table = _TEXTS.get(self._language, _TEXTS["en"])
        options = table.get(key, _TEXTS["en"][key])
        assert isinstance(options, dict)
        return str(options[choice]).format(**fields)

    async def run(
        self, verb: str, args: list[str], session_id: str | None = None
    ) -> dict[str, Any]:
        handler = getattr(self, f"_do_{verb}", None)
        if handler is None:
            return {"text": self._say("unknown")}
        try:
            answer: dict[str, Any] = await handler(args, session_id)
        except ConfigRejected as e:
            return {"text": self._say("failed", error=e)}
        return answer

    async def _apply(self, cfg: Config) -> None:
        await apply_config(self._state.app, cfg)

    async def watch(self) -> None:
        """Keep up with the panel, and pass on warnings when nobody is there to hear them."""
        async with self._bus.subscribe() as q:
            while True:
                event = await q.get()
                payload = event.payload
                if event.kind is EventKind.PLAYBACK_STATE:
                    for key in ("playback_rate", "volume", "muted"):
                        if key in payload:
                            self._panel[key] = payload[key]
                elif (
                    event.kind is EventKind.SUPERVISOR_VERDICT
                    and payload.get("status") in ("warn", "stop")
                    and not payload.get("repeat")
                    and not self._hub.has_clients()
                ):
                    message = self._say("warned", message=payload.get("message"))
                    self._adapter.notify(payload.get("session_id"), message)

    # -------------------------------------------------------------- the panel

    def _no_panel(self) -> str:
        return "" if self._hub.has_clients() else self._say("no_panel")

    async def _tell_panel(self, **settings: Any) -> None:
        self._state.panel_overrides.update(settings)
        await self._bus.publish(
            Event(kind=EventKind.PANEL_COMMAND, source="commands", payload=settings)
        )

    async def _do_speed(self, args: list[str], _sid: str | None) -> dict[str, Any]:
        low, high = _SPEED_RANGE
        if not args:
            return {
                "text": self._say(
                    "speed_now", value=self._panel["playback_rate"], low=low, high=high
                )
            }
        value = float(args[0].rstrip("xX").replace(",", "."))
        if not low <= value <= high:
            return {"text": self._say("speed_range", low=low, high=high)}
        self._panel["playback_rate"] = value
        await self._tell_panel(speed=value)
        return {"text": self._say("speed_set", value=value, no_panel=self._no_panel())}

    async def _do_volume(self, args: list[str], _sid: str | None) -> dict[str, Any]:
        if not args:
            return {"text": self._say("volume_now", value=self._panel["volume"])}
        value = int(args[0].rstrip("%"))
        if value > 100:
            return {"text": self._say("volume_range")}
        self._panel["volume"] = value
        await self._tell_panel(volume=value)
        return {"text": self._say("volume_set", value=value, no_panel=self._no_panel())}

    async def _do_mute(self, _args: list[str], _sid: str | None) -> dict[str, Any]:
        self._panel["muted"] = True
        await self._tell_panel(muted=True)
        return {"text": self._say("muted", no_panel=self._no_panel())}

    async def _do_unmute(self, _args: list[str], _sid: str | None) -> dict[str, Any]:
        self._panel["muted"] = False
        await self._tell_panel(muted=False)
        return {"text": self._say("unmuted", no_panel=self._no_panel())}

    async def _do_settings(self, _args: list[str], _sid: str | None) -> dict[str, Any]:
        url = f"{self._panel_url}/settings"
        await asyncio.to_thread(webbrowser.open, url)
        return {"text": self._say("settings", url=url)}

    # --------------------------------------------------------------- the agent

    async def _do_pause(self, _args: list[str], _sid: str | None) -> dict[str, Any]:
        if self._adapter.is_paused:
            return {"text": self._say("paused_already")}
        reply = self._say("paused" if self._chat_replies else "paused_proxy")
        if self._chat_replies:
            await self._toggle_pause()
        else:
            # The process is frozen at once, and the CLI still has to show this reply.
            pause = asyncio.create_task(self._toggle_pause_soon(), name="commands.pause")
            self._answers.add(pause)
            pause.add_done_callback(self._answers.discard)
        return {"text": reply}

    async def _do_go(self, _args: list[str], _sid: str | None) -> dict[str, Any]:
        if not self._adapter.is_paused:
            return {"text": self._say("not_paused")}
        await self._toggle_pause()
        return {"text": self._say("resumed")}

    async def _toggle_pause(self) -> None:
        await self._bus.publish(Event(kind=EventKind.USER_PAUSE_TOGGLE, source="commands"))

    async def _toggle_pause_soon(self) -> None:
        await asyncio.sleep(1.0)
        await self._toggle_pause()

    async def _do_check(self, _args: list[str], sid: str | None) -> dict[str, Any]:
        target = sid or self._active_session()
        if target is None:
            return {"text": self._say("no_session")}
        await self._bus.publish(
            Event(kind=EventKind.USER_CHECK, source="commands", payload={"session_id": target})
        )
        return {"text": self._say("check")}

    # ------------------------------------------------------------- the models

    async def _do_model(self, args: list[str], _sid: str | None) -> dict[str, Any]:
        return await self._pick_model(args, for_supervisor=False)

    async def _choices(self) -> list[str]:
        cli = self._active_cli()
        own = await listed_models(cli) if cli else []
        local = [f"ollama/{name}" for name in await ollama_models()]
        return [*own, *local]

    async def _pick_model(self, args: list[str], *, for_supervisor: bool) -> dict[str, Any]:
        choices = await self._choices()
        models = self._commentator.models()
        current = models["supervisor" if for_supervisor else "narrator"]
        if not args:
            if not choices:
                return {"text": self._say("no_list")}
            listing = "\n".join(
                f"  {i}. {name}{' *' if name == current else ''}"
                for i, name in enumerate(choices[:_LISTED_MODELS], 1)
            )
            if len(choices) > _LISTED_MODELS:
                listing += f"\n  ... {len(choices) - _LISTED_MODELS} more"
            key = "review_model_now" if for_supervisor else "model_now"
            return {
                "text": self._say(key, current=current, listing=listing),
                "choices": choices,
                "pick": "vc review model" if for_supervisor else "vc model",
            }
        name = args[0]
        if name.isdigit():
            if not 1 <= int(name) <= len(choices):
                return {"text": self._say("no_such_number", value=name)}
            name = choices[int(name) - 1]
        return await self._set_model(name, known=choices, for_supervisor=for_supervisor)

    async def _set_model(
        self, name: str, *, known: list[str], for_supervisor: bool
    ) -> dict[str, Any]:
        cli = self._active_cli()
        cfg: Config = self._state.config.model_copy(deep=True)
        provider, _, model = name.partition("/")
        own = name in known and provider != "ollama"
        third_party = not own and model and (provider == "ollama" or provider in _KEYS)
        if third_party and provider in _KEYS and not get_secret(_KEYS[provider]):
            url = f"{self._panel_url}/settings"
            await asyncio.to_thread(webbrowser.open, url)
            return {"text": self._say("needs_key", name=name, url=url)}
        if for_supervisor:
            # The supervisor shares the narrator's provider, and only its model differs.
            over = cfg.commentator.per_cli.get(cli) if cli else None
            if over is not None:
                over.supervisor_model = model if third_party else name
            cfg.commentator.supervisor.model = model if third_party else name
            await self._apply(cfg)
            return {"text": self._say("review_model_set", name=name)}
        if third_party:
            options: dict[str, str | int | float | bool] = {"model": model}
            kind = "openai-compat" if provider == "ollama" else provider
            cfg.commentator.provider = ProviderConfig(name=kind, options=options)
            if cli:
                over = cfg.commentator.per_cli.setdefault(cli, CommentatorCliOverride())
                over.mode, over.model = "api", None
            else:
                cfg.commentator.mode = "api"
        elif cli:
            over = cfg.commentator.per_cli.setdefault(cli, CommentatorCliOverride())
            over.mode, over.model = "current", name
        else:
            return {"text": self._say("no_cli")}
        await self._apply(cfg)
        return {"text": self._say("model_set", name=name)}

    # ------------------------------------------------- how much it does and sees

    async def _do_review(self, args: list[str], _sid: str | None) -> dict[str, Any]:
        if args and args[0] == "model":
            return await self._pick_model(args[1:], for_supervisor=True)
        if not args:
            return {
                "text": self._say("review_now", value=self._commentator.supervisor_mode),
                "choices": ["off", "watch", "guard"],
                "pick": "vc review",
            }
        cfg: Config = self._state.config.model_copy(deep=True)
        cfg.commentator.supervisor.mode = args[0]  # type: ignore[assignment]
        cli = self._active_cli()
        over = cfg.commentator.per_cli.get(cli) if cli else None
        if over is not None:
            over.supervisor_mode = "default"
        await self._apply(cfg)
        return {"text": self._say_choice("review_set", args[0])}

    async def _do_detail(self, args: list[str], _sid: str | None) -> dict[str, Any]:
        cfg: Config = self._state.config.model_copy(deep=True)
        if not args:
            value = _IMPORTANCE_TO_DETAIL[cfg.commentator.min_importance]
            return {
                "text": self._say("detail_now", value=value),
                "choices": ["low", "normal", "high"],
                "pick": "vc detail",
            }
        cfg.commentator.min_importance = _DETAIL_TO_IMPORTANCE[args[0]]  # type: ignore[assignment]
        await self._apply(cfg)
        return {"text": self._say("detail_set", value=args[0])}

    async def _do_access(self, args: list[str], _sid: str | None) -> dict[str, Any]:
        cfg: Config = self._state.config.model_copy(deep=True)
        if not args:
            return {
                "text": self._say("access_now", value=cfg.commentator.file_access),
                "choices": ["events", "edits", "files"],
                "pick": "vc access",
            }
        cfg.commentator.file_access = args[0]  # type: ignore[assignment]
        await self._apply(cfg)
        model = self._commentator.models()["supervisor"]
        return {"text": self._say_choice("access_set", args[0], model=model)}

    # ------------------------------------------------------- what it has to say

    async def _do_log(self, _args: list[str], _sid: str | None) -> dict[str, Any]:
        lines = self._commentator.recent_lines(_LOG_LINES)
        return {"text": "\n".join(lines) if lines else self._say("log_empty")}

    async def _do_cost(self, _args: list[str], _sid: str | None) -> dict[str, Any]:
        usage: Counter[str] = self._commentator.usage()
        return {
            "text": self._say(
                "cost",
                narrator=usage["narrator.calls"],
                summary=usage["summary.calls"],
                supervisor=usage["supervisor.calls"],
                question=usage["question.calls"],
                tokens_in=_thousands(sum(v for k, v in usage.items() if k.endswith(".in"))),
                tokens_out=_thousands(sum(v for k, v in usage.items() if k.endswith(".out"))),
            )
        }

    async def _do_ask(self, args: list[str], sid: str | None) -> dict[str, Any]:
        task = asyncio.create_task(self._answer(args[0], sid), name="commands.ask")
        self._answers.add(task)
        task.add_done_callback(self._answers.discard)
        model = self._commentator.models()["supervisor"]
        return {"text": self._say("asking" if self._chat_replies else "asking_proxy", model=model)}

    async def _answer(self, question: str, sid: str | None) -> None:
        try:
            text = await self._commentator.answer_question(question, sid or self._active_session())
        except Exception as e:
            log.exception("vc ask failed")
            text = self._say("answer_failed", error=e)
        self._adapter.notify(sid, text)

    async def _do_test(self, _args: list[str], _sid: str | None) -> dict[str, Any]:
        await self._commentator.say_line(self._say("test_line"))
        return {"text": self._say("tested")}


def _thousands(chars: int) -> str:
    return f"{chars / _CHARS_PER_TOKEN / 1000:.1f}"
