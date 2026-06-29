from voice_copilot.commentator.provider_select import (
    commentator_status_text,
    resolve_commentator_provider,
)
from voice_copilot.core.config import CommentatorCliOverride, CommentatorConfig, ProviderConfig


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
