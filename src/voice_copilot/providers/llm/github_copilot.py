"""GitHub Copilot LLM provider.

OpenAI-compatible endpoint at https://api.githubcopilot.com.
Supported models (as of 2025): gpt-4o, gpt-4o-mini, gpt-4.1, gpt-4.1-mini,
gpt-4.1-nano, gpt-5-mini, o1, o3-mini, claude-3.5-sonnet, …

Token resolution order:
  1. ``GITHUB_COPILOT_TOKEN`` in OS keychain (set via the API keys tab).
  2. ``GITHUB_COPILOT_TOKEN`` environment variable.
  3. ``gh auth token`` — GitHub CLI (fastest if already logged in).
  4. ``oauth_token`` from local GitHub Copilot config file written by VS Code /
     ``gh auth login``:
       Windows  : %LOCALAPPDATA%\\github-copilot\\hosts.json
       macOS/Linux: ~/.config/github-copilot/hosts.json
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from voice_copilot.core.secrets import get_secret
from voice_copilot.providers.llm.openai import OpenAIProvider
from voice_copilot.providers.registry import register

log = logging.getLogger(__name__)

_BASE_URL = "https://api.githubcopilot.com"
_EXTRA_HEADERS = {
    "Copilot-Integration-Id": "vscode-chat",
    "Editor-Version": "vscode/1.99.0",
    "Editor-Plugin-Version": "copilot-chat/0.22.0",
}


def _hosts_json_paths() -> list[Path]:
    """Candidate paths for GitHub Copilot's hosts.json across platforms."""
    candidates: list[Path] = []
    if os.name == "nt":  # Windows
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            candidates.append(Path(local) / "github-copilot" / "hosts.json")
    home = Path.home()
    candidates.append(home / ".config" / "github-copilot" / "hosts.json")
    candidates.append(home / "AppData" / "Local" / "github-copilot" / "hosts.json")
    return candidates


def _token_from_gh_cli() -> str | None:
    """Run `gh auth token` and return the current GitHub OAuth token."""
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            token = result.stdout.strip()
            if token:
                log.info("github-copilot: token obtained via `gh auth token`")
                return token
        log.debug("github-copilot: gh auth token failed: %s", result.stderr.strip())
    except FileNotFoundError:
        log.debug("github-copilot: `gh` CLI not found on PATH")
    except Exception as e:
        log.debug("github-copilot: gh auth token error: %s", e)
    return None


def _token_from_hosts_json() -> str | None:
    """Try to read the OAuth token from the local Copilot credential store."""
    for path in _hosts_json_paths():
        try:
            if not path.exists():
                continue
            data: Any = json.loads(path.read_text(encoding="utf-8"))
            # Structure: {"github.com": {"oauth_token": "ghu_..."}}
            if not isinstance(data, dict):
                continue
            for host_data in data.values():
                if isinstance(host_data, dict):
                    token = host_data.get("oauth_token")
                    if isinstance(token, str) and token:
                        log.info("github-copilot: token auto-discovered from %s", path)
                        return token
        except Exception as e:
            log.debug("github-copilot: could not read %s: %s", path, e)
    return None


def _discover_token() -> str | None:
    return _token_from_gh_cli() or _token_from_hosts_json()


@register("llm", "github-copilot")
class GitHubCopilotProvider(OpenAIProvider):
    """Commentator LLM backed by GitHub Copilot Chat API."""

    name = "github-copilot"

    def __init__(
        self,
        model: str = "gpt-4.1-mini",
        api_key: str | None = None,
    ) -> None:
        # Resolve token now; if missing, _get_client() will raise when first used.
        # Do NOT raise here — a missing token must not crash the app at startup
        # (e.g. when the user has this provider saved but hasn't set the token yet).
        token = api_key or get_secret("GITHUB_COPILOT_TOKEN") or _discover_token()
        super().__init__(model=model, api_key=token or "", base_url=_BASE_URL)
        self._extra_headers = dict(_EXTRA_HEADERS)
        self._token_missing = not token

    def _get_client(self) -> Any:
        if self._token_missing:
            raise RuntimeError(
                "GitHub Copilot token not found. Tried: keychain, env, "
                "`gh auth token`, hosts.json. Fix: run `gh auth login` "
                "or set GITHUB_COPILOT_TOKEN in the API keys tab."
            )
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
                default_headers=self._extra_headers,
            )
        return self._client
