# Importing voice_copilot.hotkeys first selects pynput's no-op backend on a
# headless Linux runner (no DISPLAY), so test modules that import pynput
# directly collect instead of failing with "failed to acquire X connection".
import voice_copilot.hotkeys  # noqa: F401
