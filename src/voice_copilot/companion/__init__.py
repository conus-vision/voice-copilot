"""Attach to a CLI the user started themselves, through its plugin hooks.

Keep this package's import light: `hook_forward` runs on every tool call of the
agent, so it must not pull in the server, the providers or pydantic.
"""
