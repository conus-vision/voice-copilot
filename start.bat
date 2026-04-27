@echo off
setlocal

set "ROOT=%~dp0"
pushd "%ROOT%" >nul || exit /b 1

if "%~1"=="" (
    call :run serve
) else (
    call :run %*
)

set "EXITCODE=%ERRORLEVEL%"
popd >nul
exit /b %EXITCODE%

:run
where /Q uv
if not errorlevel 1 (
    uv run voice-copilot %*
    exit /b %ERRORLEVEL%
)

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m voice_copilot %*
    exit /b %ERRORLEVEL%
)

where /Q voice-copilot
if not errorlevel 1 (
    voice-copilot %*
    exit /b %ERRORLEVEL%
)

echo Neither "uv", local ".venv\Scripts\python.exe", nor "voice-copilot" was found.
echo Install dependencies with "uv sync" or "pipx install voice-copilot".
exit /b 1