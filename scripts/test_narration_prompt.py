"""Smoke-test for narration prompts via copilot-cli.

Run from repo root:
    uv run python scripts/test_narration_prompt.py

Tests the ACTUAL production prompt (ru.cli.md + build_narration_user)
against copilot-cli subprocess, then verifies the response is a real
narration (not a role-acceptance or generic description).
"""

import subprocess
import sys
import textwrap

COPILOT = "copilot"

# Sample events representing two realistic scenarios.
SCENARIOS = {
    "thinking only": {
        "user_query": "Привет! Расскажи какие программы можно написать через вайб кодинг",
        "already": "Агент изучил репозиторий voice-copilot и готов к работе.",
        "events_md": "- agent thinking: about Vibe Coding categories: Web/SaaS, Automation, Data, Games",
    },
    "thinking + answer": {
        "user_query": "Привет! Расскажи какие программы можно написать через вайб кодинг",
        "already": "Агент рассмотрел категории вайб-кодинга.",
        "events_md": (
            "- agent thinking: about Vibe Coding categories: Web/SaaS, Automation, Data, Games\n"
            "- agent said: Vibe coding — це підхід, де ви фокусуєтеся на архітектурі."
        ),
    },
    "tool + file edit": {
        "user_query": "Добавь функцию hello() в main.py",
        "already": "(nothing yet)",
        "events_md": (
            "- tool Read started: main.py\n- tool Read ok: main.py\n- file edited: main.py"
        ),
    },
}


def prompt_section_style(user_query: str, already: str, events_md: str) -> str:
    """Current style: 'прочитай секцию [NEW_EVENTS]' — triggers file search in -p mode."""
    from pathlib import Path

    system = (
        (Path(__file__).parent.parent / "src/voice_copilot/commentator/prompts/ru.cli.md")
        .read_text(encoding="utf-8")
        .strip()
    )
    has_thinking = "agent thinking:" in events_md
    has_answer = "agent said:" in events_md or "turn ended" in events_md
    if has_answer and not has_thinking:
        hint = "[NEW_EVENTS] содержит финальный ответ агента."
    elif has_thinking and not has_answer:
        hint = "[NEW_EVENTS] содержит размышления агента (ещё не ответил)."
    elif has_thinking and has_answer:
        hint = "[NEW_EVENTS] содержит размышления и финальный ответ агента."
    else:
        hint = "[NEW_EVENTS] содержит действия агента."
    user = "\n".join(
        [
            "[USER_QUERY]",
            user_query,
            "",
            "[ALREADY_DONE_AND_SAID]",
            already,
            "",
            "[NEW_EVENTS]",
            events_md,
            "",
            f"{hint} Ответ (1-2 предложения прозы, только по [NEW_EVENTS]):",
        ]
    )
    return f"{system}\n\n{user}"


def prompt_no_section(user_query: str, already: str, events_md: str) -> str:
    """Avoids 'read section' language — events presented inline as data."""
    has_thinking = "agent thinking:" in events_md
    has_answer = "agent said:" in events_md or "turn ended" in events_md
    if has_answer and not has_thinking:
        hint = "Ниже — финальный ответ агента."
    elif has_thinking and not has_answer:
        hint = "Ниже — обдумывание агента (ещё не ответил)."
    else:
        hint = "Ниже — обдумывание и финальный ответ агента."

    return "\n".join(
        [
            "ЗАДАЧА: напиши 1-2 предложения русской прозы для голосового озвучивания.",
            "Используй ТОЛЬКО события ниже. Не выдумывай.",  # noqa: RUF001
            "Легенда: «agent thinking:» = агент думает; «agent said:» = финальный ответ.",
            "",
            f"Пользователь спросил: {user_query}",
            f"Уже озвучено: {already}",
            "",
            f"События: {hint}",
            events_md,
            "",
            "Ответ (1-2 предложения прозы, без markdown): Агент",
        ]
    )


def run_p_flag(prompt: str, model: str) -> str:
    """Run via `copilot -p '...'` (non-interactive)."""
    result = subprocess.run(
        [
            "cmd.exe",
            "/C",
            COPILOT,
            "--model",
            model,
            "--allow-all",
            "--no-auto-update",
            "-s",
            "-p",
            prompt,
        ],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    return (f"[stderr] {err[:300]}" if not out and err else out) or "[empty]"


def run_stdin(prompt: str, model: str) -> str:
    """Feed prompt via stdin to interactive copilot session."""
    proc = subprocess.Popen(
        ["cmd.exe", "/C", COPILOT, "--model", model, "--allow-all", "--no-auto-update", "-s"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        out, err = proc.communicate(
            input=prompt.encode("utf-8"),
            timeout=60,
        )
        text = out.decode("utf-8", errors="replace").strip()
        return text or f"[stderr] {err.decode('utf-8', errors='replace')[:300]}"
    except subprocess.TimeoutExpired:
        proc.kill()
        return "[timeout]"


def grade(response: str, scenario: dict) -> str:
    """Simple heuristic check."""
    if not response or len(response) < 20:
        return "FAIL (too short)"
    bad_phrases = [
        "пожалуйста, вставьте",
        "please provide",
        "how can i help",
        "принято",
        "буду комментатором",
        "пришлите события",
        "укажите задачу",
        "не указали",
    ]
    low = response.lower()
    for phrase in bad_phrases:
        if phrase in low:
            return f"FAIL (role-acceptance: '{phrase}')"
    # Check that at least one keyword from events is echoed
    events = scenario["events_md"].lower()
    keywords = []
    if "vibe cod" in events:
        keywords = ["vibe", "вайб", "кодинг"]
    elif "main.py" in events:
        keywords = ["main.py", "файл", "изменил", "добавил"]
    if keywords and not any(k in low for k in keywords):
        return "WARN (no event keywords in response)"
    return "PASS"


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

    # Use one representative scenario for the matrix test.
    def production_prompt(user_query: str, already: str, events_md: str) -> str:
        """Use the exact same code path as the running commentator."""
        from pathlib import Path

        from voice_copilot.commentator.format import build_narration_user
        from voice_copilot.commentator.prompts import load, load_summary  # noqa: F401
        from voice_copilot.core.events import Event, EventKind

        system = (
            (Path(__file__).parent.parent / "src/voice_copilot/commentator/prompts/ru.cli.md")
            .read_text(encoding="utf-8")
            .strip()
        )
        # Fake events from text
        evs: list[Event] = []
        for line in events_md.splitlines():
            if "agent thinking:" in line:
                evs.append(
                    Event(
                        kind=EventKind.AGENT_THINKING,
                        source="t",
                        payload={"text": line.split("agent thinking:")[-1].strip()},
                    )
                )
            elif "agent said:" in line:
                evs.append(
                    Event(
                        kind=EventKind.AGENT_TEXT,
                        source="t",
                        payload={"text": line.split("agent said:")[-1].strip()},
                    )
                )
        user_msg = build_narration_user(
            user_query=user_query, summary=already, events=evs, style="cli"
        )
        return f"{system}\n\n{user_msg}"

    RUNS = [
        ("production prompt + stdin", production_prompt, run_stdin, "gpt-5-mini"),
        ("no-section       + stdin", prompt_no_section, run_stdin, "gpt-5-mini"),
    ]

    sep = "-" * 70
    total = passed = 0

    for scenario_name, _ in SCENARIOS.items():
        scenario = SCENARIOS[scenario_name]
        print(f"\n{'=' * 70}")
        print(f"SCENARIO: {scenario_name}")

        for run_name, pfn, rfn, model in RUNS:
            prompt = pfn(scenario["user_query"], scenario["already"], scenario["events_md"])
            print(f"\n{sep}")
            print(f"VARIANT: {run_name}  model={model}")
            response = rfn(prompt, model)
            grade_result = grade(response, scenario)
            total += 1
            if grade_result.startswith("PASS"):
                passed += 1
            print(f"GRADE: {grade_result}")
            print(f"RESPONSE:\n{textwrap.indent(response[:300], '  ')}")

    print(f"\n{'=' * 70}")
    print(f"RESULT: {passed}/{total} passed")


if __name__ == "__main__":
    main()
