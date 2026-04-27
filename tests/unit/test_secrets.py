from types import SimpleNamespace

from voice_copilot.core import secrets


def test_get_secret_prefers_environment(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    monkeypatch.setattr(secrets, "_kr", lambda: None)

    assert secrets.get_secret("ANTHROPIC_API_KEY") == "from-env"


def test_get_secret_reads_keyring_when_environment_is_missing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    keyring = SimpleNamespace(get_password=lambda service, name: f"{service}:{name}")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(secrets, "_kr", lambda: keyring)

    assert secrets.get_secret("OPENAI_API_KEY") == "voice-copilot:OPENAI_API_KEY"


def test_set_secret_requires_keyring(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(secrets, "_kr", lambda: None)

    try:
        secrets.set_secret("OPENAI_API_KEY", "secret")
    except RuntimeError as exc:
        assert "keyring backend" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected RuntimeError")


def test_list_known_present_never_returns_values(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        secrets, "get_secret", lambda name: "secret" if name == "OPENAI_API_KEY" else None
    )

    present = secrets.list_known_present()

    assert present["OPENAI_API_KEY"] is True
    assert all(isinstance(value, bool) for value in present.values())
