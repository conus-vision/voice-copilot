def remote_control_gate_message(source: str) -> str:
    """Return the shared Remote Control compatibility message for Claude warning paths."""
    source_clean = source.strip() or "this endpoint"
    return REMOTE_CONTROL_DISABLED_MESSAGE.format(source=source_clean)
