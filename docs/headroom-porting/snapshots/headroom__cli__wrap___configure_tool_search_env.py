def _configure_tool_search_env(env: dict[str, str], flag_value: str | None) -> str | None:
    """Set ``ENABLE_TOOL_SEARCH`` in ``env`` so Claude Code keeps deferring tools.

    Precedence:

    1. explicit ``--tool-search`` flag — wins (the user asked for it on the CLI),
    2. a pre-existing ``ENABLE_TOOL_SEARCH`` in the environment — respected and
       left untouched (the user's own Claude Code knob),
    3. the built-in default (``true``).

    Returns the value written, or ``None`` when an existing environment value
    was deliberately left in place.
    """
    if flag_value is not None:
        value = _normalize_tool_search_mode(flag_value)
        env[_TOOL_SEARCH_ENV] = value
        return value
    # An empty / whitespace value counts as unset: Claude Code treats an empty
    # ENABLE_TOOL_SEARCH as absent (so deferral would stay off), so we override
    # it with the default rather than forwarding a no-op value.
    existing = env.get(_TOOL_SEARCH_ENV)
    if existing is not None and existing.strip():
        return None
    env[_TOOL_SEARCH_ENV] = _TOOL_SEARCH_DEFAULT
    return _TOOL_SEARCH_DEFAULT
