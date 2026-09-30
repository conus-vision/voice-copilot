"""Coding CLIs report to Voice Copilot through their own plugin and hook systems.

* ``hub``: turns those reports into bus events and answers back (pause,
  Supervisor STOP, voice messages).
* ``dialects``: the hook formats (Claude Code's and the CLIs that copied it,
  Gemini CLI, Copilot CLI).
* ``routes``: the ``/api/companion/v1`` HTTP endpoints.
* ``hook``: the ``voice-copilot-hook`` forwarder a CLI runs per hook.
* ``integrations``: installing the plugins and wiring them into a launch.
* ``assets/``: the plugins themselves (Pi, OpenCode, Hermes).

Nothing is imported here: the forwarder starts once per hook and must stay fast.
"""
