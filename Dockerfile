# Dockerfile for voice-copilot.
#
# CAVEATS — read before running:
#   1. Microphone: the browser only grants `getUserMedia` on trusted origins.
#      That means `localhost`. Running the server in a container and opening
#      the popup from the host works ONLY if you use `--network host` (Linux)
#      or terminate HTTPS in front of the container. On Docker Desktop for
#      Mac/Windows, the host sees the server as `localhost:8765` via port
#      forwarding — modern Chromium treats that as a secure context, so mic
#      access usually works. Firefox is stricter.
#   2. Target CLIs: `claude`, `codex`, etc. are NOT installed in this image.
#      You'd typically mount your workspace and install them inside, or run
#      only `voice-copilot serve` and talk to a CLI running on the host.
#   3. Global hotkeys: `pynput` needs an X server / input device on Linux. In
#      a headless container they silently no-op. Use the in-popup buttons.
#   4. Tray icon: `pystray` is skipped when `--tray=false` is passed.
#
# In short: the container is most useful for `voice-copilot serve --demo` and
# for the `proxy` mode; for full voice-paired coding, install on the host.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY voice_copilot ./voice_copilot

RUN pip install --upgrade pip && pip install .

# Keyring needs a backend in the container. Fall back to a plaintext file
# backend — keys still don't leave the container image because nothing is
# baked in; operators mount /root/.local/share/python_keyring at runtime.
RUN pip install keyrings.alt

EXPOSE 8765 8766

ENV VOICE_COPILOT_HOST=0.0.0.0 \
    VOICE_COPILOT_PORT=8765

# Default entrypoint — demo mode, no hotkeys, no tray.
ENTRYPOINT ["voice-copilot"]
CMD ["serve", "--host", "0.0.0.0", "--no-open", "--no-hotkeys", "--no-tray", "--demo"]
