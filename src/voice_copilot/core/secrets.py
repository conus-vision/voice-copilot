"""Secrets store backed by the OS keychain (Windows Credential Manager,
macOS Keychain, Secret Service on Linux) via the `keyring` package.

Callers ask for a secret by logical name (e.g. ``ANTHROPIC_API_KEY``).
Resolution order when reading:

1. Process environment (so `.env` or shell exports still win — handy for dev).
2. OS keychain under service ``voice-copilot``.
3. Missing → ``None``.

Writing always goes through the keychain; we never persist secrets to the
YAML config. Listing returns only *names*, never values.
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

SERVICE = "voice-copilot"

KNOWN_SECRETS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "DEEPGRAM_API_KEY",
    "ELEVENLABS_API_KEY",
    "OPENAI_COMPAT_API_KEY",
    "GITHUB_COPILOT_TOKEN",
)


def _kr() -> Any | None:
    try:
        import keyring

        return keyring
    except Exception as e:
        log.warning("keyring unavailable: %s", e)
        return None


def get_secret(name: str) -> str | None:
    env_val = os.environ.get(name)
    if env_val:
        return env_val
    kr = _kr()
    if kr is None:
        return None
    try:
        value = kr.get_password(SERVICE, name)
        return str(value) if value is not None else None
    except Exception as e:
        log.warning("keyring read %s failed: %s", name, e)
        return None


def set_secret(name: str, value: str) -> None:
    kr = _kr()
    if kr is None:
        raise RuntimeError("keyring backend not available on this system")
    kr.set_password(SERVICE, name, value)


def delete_secret(name: str) -> None:
    kr = _kr()
    if kr is None:
        return
    try:
        kr.delete_password(SERVICE, name)
    except Exception as e:
        # `keyring` raises PasswordDeleteError when the entry is missing — fine.
        log.debug("keyring delete %s: %s", name, e)


def list_known_present() -> dict[str, bool]:
    """Return {name: is_set} for each secret we care about. Values never leave."""
    return {n: get_secret(n) is not None for n in KNOWN_SECRETS}
