"""ConPTY's win32-input-mode request must not escape to the real console.

ConPTY emits `CSI ? 9001 h` to ask its host for Windows input records. The
Windows pump forwards the child's output verbatim, so that request used to
reach our own conhost, which obliged — `ReadConsoleW` then returned
`ESC[Vk;Sc;Uc;Kd;Cs;Rc_` records that the feeder passed straight back to the
child, which drew them as literal text instead of the typed letter.
"""

from voice_copilot.adapters.pty_adapter import _Win32InputModeFilter


def test_request_is_swallowed_and_the_rest_survives() -> None:
    f = _Win32InputModeFilter()
    assert f.feed("hello\x1b[?9001hworld") == "helloworld"
    assert f.feed("\x1b[?9001lbye") == "bye"


def test_focus_reporting_is_swallowed_too() -> None:
    # ConPTY asks the host for focus events as well; unhandled, they come back
    # as bare ESC[I / ESC[O and the child prints them into its prompt.
    f = _Win32InputModeFilter()
    assert f.feed("a\x1b[?1004hb\x1b[?1004lc") == "abc"


def test_modes_the_child_owns_pass_through() -> None:
    # Bracketed paste and the alternate screen are the child's own business.
    f = _Win32InputModeFilter()
    payload = "\x1b[?2004h\x1b[?1049h"
    assert f.feed(payload) == payload


def test_reset_disables_both_latches() -> None:
    assert _Win32InputModeFilter.RESET == "\x1b[?9001l\x1b[?1004l"


def test_sequence_split_across_reads_is_still_caught() -> None:
    f = _Win32InputModeFilter()
    # A 1024-byte read can land anywhere; the partial tail must be held back
    # rather than printed and then un-printable.
    assert f.feed("abc\x1b[?90") == "abc"
    assert f.feed("01hdef") == "def"


def test_a_partial_tail_that_turns_out_to_be_ordinary_text_is_not_lost() -> None:
    f = _Win32InputModeFilter()
    assert f.feed("x\x1b[?9") == "x"
    assert f.feed("00m") == "\x1b[?900m"


def test_esc_at_the_very_end_is_held_then_released() -> None:
    f = _Win32InputModeFilter()
    assert f.feed("done\x1b") == "done"
    assert f.feed("[2J") == "\x1b[2J"
