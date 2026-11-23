"""The case file: facts the end-of-work review and the advisor can rely on."""

from __future__ import annotations

from voice_copilot.commentator.casefile import CaseFile, check_kind, parse_advice


def test_check_kinds_come_from_what_runs() -> None:
    assert check_kind("uv run pytest tests/unit -x") == "tests"
    assert check_kind(["bash", "-lc", "npm test"]) == "tests"
    assert check_kind("npx tsc --noEmit") == "types"
    assert check_kind("ruff check src") == "lint"
    assert check_kind("cargo build --release") == "build"
    assert check_kind('grep -rn "pytest" .') is None
    assert check_kind("git status") is None
    assert check_kind(None) is None


def _render(case: CaseFile) -> str:
    return case.render(goal="fix the parser", progress="2 min", files=["parser.py"])


def test_a_check_that_passed_after_the_last_edit_covers_it() -> None:
    case = CaseFile()
    case.tick()
    case.edited()
    case.tick()
    case.started("t1", "pytest -q")
    case.tick()
    case.finished("t1", failed=False)
    assert "last passed, after the latest edit" in _render(case)


def test_an_edit_after_the_check_is_not_covered() -> None:
    case = CaseFile()
    case.tick()
    case.started("t1", "pytest -q")
    case.finished("t1", failed=False)
    case.tick()
    case.edited()
    assert "BEFORE the latest edit" in _render(case)


def test_a_failed_rerun_is_what_counts() -> None:
    case = CaseFile()
    for failed in (False, True):
        case.tick()
        case.started(None, "pytest -q")
        case.finished(None, failed=failed)
    assert "ran 2 times; last FAILED" in _render(case)


def test_no_checks_on_edited_code_is_said_plainly() -> None:
    assert "no tests, type check, lint or build was run" in _render(CaseFile())
    assert "Checks:" not in CaseFile().render(goal="explain", progress="1 min", files=[])


def test_held_commands_what_was_said_and_notes_are_on_file() -> None:
    case = CaseFile()
    case.held.append("the user said: don't run it (git push --force); it did not run")
    case.told.append("WARN: tests were not run")
    text = _render(case)
    assert "Supervisor+ held: the user said: don't run it" in text
    assert "Already told the user: WARN: tests were not run" in text


def test_advice_is_read_leniently_and_never_invents_a_note() -> None:
    reply = (
        "**WATCH:** plaintext passwords\n- WATCH: email uniqueness\n"
        "NOTE: Пароль сохраняется как есть.\nSEND: Хешируй пароль через bcrypt."
    )
    assert parse_advice(reply) == (
        ["plaintext passwords", "email uniqueness"],
        "Пароль сохраняется как есть.",
        "Хешируй пароль через bcrypt.",
    )
    assert parse_advice("WATCH: a\nOK") == (["a"], None, None)
    assert parse_advice("Looks fine to me.") == ([], None, None)
    assert parse_advice("NOTE: check the scope\nSEND: none") == ([], "check the scope", None)
    assert parse_advice("SEND: do something") == ([], None, None)  # no note, nothing to send
