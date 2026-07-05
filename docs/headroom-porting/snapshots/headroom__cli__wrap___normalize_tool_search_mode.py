def _normalize_tool_search_mode(value: str) -> str:
    """Validate an ``ENABLE_TOOL_SEARCH`` value and return it normalized.

    Mirrors the values Claude Code accepts: truthy (``true``/``1``/``yes``/
    ``on``), falsy (``false``/``0``/``no``/``off``), ``auto``, or ``auto:N``
    where ``N`` is 0-100. Raises :class:`click.ClickException` on anything else
    so a typo fails loudly instead of silently leaving deferral off.
    """
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "on", "false", "0", "no", "off", "auto"}:
        return normalized
    if normalized.startswith("auto:"):
        suffix = normalized[len("auto:") :]
        if suffix.isdigit() and 0 <= int(suffix) <= 100:
            return normalized
    raise click.ClickException(
        f"--tool-search must be one of: true, false, auto, auto:N (N 0-100); got {value!r}"
    )
