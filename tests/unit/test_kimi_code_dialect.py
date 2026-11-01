"""Kimi Code speaks the Claude hook format with two differences.

Its PostToolUse hook runs without waiting for an answer and it reads no
additionalContext, and it continues after a Stop hook only on a "deny",
feeding the model the reason. Checked against Kimi Code 2.1.1: a voice
message given as `decision: block` never reached the model.
"""

from __future__ import annotations

from voice_copilot.companion import dialects


def _call(event: str, cli: str) -> dialects.HookCall:
    return dialects.parse("claude", cli, {"hook_event_name": event, "session_id": "s"}, None)


def test_kimi_takes_a_voice_message_at_the_end_of_the_turn() -> None:
    reply = dialects.keep_going(_call("Stop", "kimi"), "Also count the lines.")
    assert reply == {
        "hookSpecificOutput": {
            "hookEventName": "Stop",
            "permissionDecision": "deny",
            "permissionDecisionReason": "Also count the lines.",
        }
    }
    # Claude Code keeps its own format.
    assert dialects.keep_going(_call("Stop", "claude"), "x") == {"decision": "block", "reason": "x"}


def test_kimi_tool_hooks_carry_no_context() -> None:
    assert dialects.add_context(_call("PostToolUse", "kimi"), "hi") is None
    assert dialects.add_context(_call("PostToolUse", "claude"), "hi") is not None


def test_kimi_tool_gate_is_the_claude_one() -> None:
    reply = dialects.deny(_call("PreToolUse", "kimi"), "stopped")
    assert reply["hookSpecificOutput"]["permissionDecision"] == "deny"
