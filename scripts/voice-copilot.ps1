# voice-copilot launcher for Windows PowerShell.
#
# Usage:
#   .\scripts\voice-copilot.ps1 claude -p "fix failing tests"
#   .\scripts\voice-copilot.ps1 codex  -p "explain this repo"
#   .\scripts\voice-copilot.ps1 serve
#
# Works from the project checkout (prefers `uv run`) or from a global install
# (`voice-copilot` on PATH).

$ErrorActionPreference = "Stop"

if (Get-Command uv -ErrorAction SilentlyContinue) {
    $root = Split-Path -Parent $PSScriptRoot
    Push-Location $root
    try {
        uv run voice-copilot @args
    } finally {
        Pop-Location
    }
} elseif (Get-Command voice-copilot -ErrorAction SilentlyContinue) {
    voice-copilot @args
} else {
    Write-Error "Neither 'uv' nor 'voice-copilot' found on PATH. Install with: pipx install voice-copilot"
    exit 1
}
