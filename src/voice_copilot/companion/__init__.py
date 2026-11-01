"""Coding CLIs report to Voice Copilot through their own plugin and hook systems.

Plugins that ship with the package and the `vc` words of the chat:

* ``setup``: `voice-copilot setup`, which connects the CLIs with a plugin in ``plugins/``.
* ``hook_forward``: `voice-copilot-hook`, what those plugins run.
* ``commands``, ``center``: what counts as a `vc` word, and what it does.
* ``runtime``, ``normalize``, ``transcript``, ``wake``: the companion's state and senses.

Hooks for the other CLIs, reporting over ``/api/companion/v1``:

* ``integrations``: `voice-copilot integrate`, which installs them.
* ``hook``: `voice-copilot-hub-hook`, the forwarder a CLI runs per hook.
* ``hub``, ``routes``, ``dialects``: turn those reports into bus events and answer back.
* ``assets/``: the plugins for OpenCode and Hermes.

Nothing is imported here: the forwarders run on every tool call of the agent, so they
must not pull in the server, the providers or pydantic.
"""
