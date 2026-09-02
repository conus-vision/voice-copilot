# Contributing to Voice Copilot

Thanks for helping make Voice Copilot better. The project is early, so small focused pull requests are especially valuable: bug fixes, provider improvements, tests, docs, and reports from real CLI workflows.

## Development Setup

Voice Copilot uses Python 3.11+ and `uv`.

```bash
git clone https://github.com/conus-vision/voice-copilot.git
cd voice-copilot
uv sync --extra dev
uv run voice-copilot serve --demo
```

Useful checks before opening a pull request:

```bash
uv run ruff format .
uv run ruff check .
uv run mypy src/voice_copilot
uv run pytest
```

## Pull Requests

- Keep changes focused on one problem or feature.
- Add or update tests for behavioral changes.
- Update README or docs when user-facing behavior changes.
- Do not include API keys, tokens, local config files, or generated cache directories.
- Prefer explicit errors over silent fallbacks. If a provider fails, surface the failure.

## Tests

Unit tests live under `tests/unit/`. Tests should avoid real network calls and external API providers unless they are explicitly marked as integration tests.

## Code Style

- Format with `ruff format`.
- Lint with `ruff check`.
- Keep new code compatible with strict `mypy`.
- Use typed interfaces for providers and adapters.
- Keep secrets in the OS keyring or environment variables, never in config files.

## Releasing

Releases go to PyPI through the `Publish` workflow
(`.github/workflows/publish.yml`) using PyPI trusted publishing, so no API
token lives in the repository or in CI secrets.

1. Bump `version` in `pyproject.toml` and `__version__` in
   `src/voice_copilot/__init__.py`; add the section to `CHANGELOG.md`.
2. Commit, then tag and push:

   ```bash
   git tag vX.Y.Z
   git push origin main vX.Y.Z
   ```

3. The workflow checks that the tag matches both version strings, runs the
   tests, builds the sdist and wheel, uploads them to PyPI and creates a
   GitHub release with the artifacts attached.

The PyPI page shows the README as it was at build time, so README changes
only reach PyPI with the next release.

## Reporting Issues

Please include:

- Operating system and Python version
- Voice Copilot version or commit SHA
- Target CLI and version, if relevant
- Provider configuration, without secret values
- Reproduction steps and expected behavior

Security issues should be reported privately. See `SECURITY.md`.
