#!/usr/bin/env bash
# voice-copilot launcher for macOS / Linux.
#
# Usage:
#   ./scripts/voice-copilot.sh claude -p "fix failing tests"
#   ./scripts/voice-copilot.sh codex  -p "explain this repo"
#   ./scripts/voice-copilot.sh serve
#
# Works from the project checkout (prefers `uv run`) or from a global install.

set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"

if command -v uv >/dev/null 2>&1; then
  cd "$here"
  exec uv run voice-copilot "$@"
elif command -v voice-copilot >/dev/null 2>&1; then
  exec voice-copilot "$@"
else
  echo "neither 'uv' nor 'voice-copilot' on PATH. install with: pipx install voice-copilot" >&2
  exit 1
fi
