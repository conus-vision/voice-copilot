"""The environment child processes get.

voice-copilot reads a `.env` for its own keys and settings (see
.env.example), searching upward from where it runs. Under `vc` that is the
project the agent works on, and the project's `.env` was never meant for the
agent: an ANTHROPIC_API_KEY there moved Claude Code off the user's
subscription onto API billing, and every command the agent ran saw the app's
DATABASE_URL. So `.env` values stay in this process, and children get the
user's own environment.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from dotenv import dotenv_values, find_dotenv

#: What `.env` added to os.environ, so children can be given everything else.
_from_dotenv: dict[str, str] = {}


def load_dotenv_for_self() -> None:
    """Load `.env` into this process; shell exports win over the file."""
    path = find_dotenv(usecwd=True)
    if not path:
        return
    for key, value in dotenv_values(path).items():
        if value is None or key in os.environ:
            continue
        os.environ[key] = value
        _from_dotenv[key] = value


def child_env(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    """os.environ without what `.env` put there, plus `overrides`."""
    env = {key: value for key, value in os.environ.items() if _from_dotenv.get(key) != value}
    if overrides:
        env.update(overrides)
    return env
