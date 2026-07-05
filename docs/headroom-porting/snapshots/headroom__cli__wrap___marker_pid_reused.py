def _marker_pid_reused(marker: Path, pid: int) -> bool:
    """True only if the live ``pid`` is *provably* a different process than the
    one that wrote ``marker`` (i.e. the PID was recycled after a crash).

    Conservative by design: any uncertainty (legacy marker, unknown start time,
    mismatched source) returns ``False`` so a real client is never pruned.
    """
    try:
        rec = json.loads(_read_text(marker))
    except (OSError, ValueError):
        return False
    src = rec.get("start_src")
    recorded = rec.get("start_time")
    if not isinstance(src, str) or not isinstance(recorded, int | float):
        return False  # legacy / identity-less marker — can't tell
    ident = _proc_identity(pid)
    if ident is None or ident[0] != src:
        return False  # can't compare like-for-like — don't prune
    # Start times are stable per process; >1s apart means a different process.
    return abs(ident[1] - float(recorded)) > 1.0
