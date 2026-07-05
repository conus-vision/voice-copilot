def _proc_identity(pid: int) -> tuple[str, float] | None:
    """Best-effort ``(source, start_time)`` identity for a PID.

    Used to defeat PID reuse: a marker is only trusted while the live PID is
    *the same process* that wrote it. Returns ``None`` when start time can't be
    determined (e.g. macOS without psutil), in which case callers fall back to
    existence-only liveness — no regression, just no reuse protection there.

    The ``source`` tag ("psutil" vs "proc") guards against comparing values in
    different units; we only compare like-for-like.
    """
    try:
        import psutil  # type: ignore[import-untyped]  # optional dependency; portable when present

        return ("psutil", psutil.Process(pid).create_time())
    except Exception:
        pass
    # Linux fallback: field 22 of /proc/<pid>/stat is starttime in clock ticks
    # since boot — a stable per-process value. `comm` (field 2) may contain
    # spaces/parens, so split after the final ')'.
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            fields = fh.read().rpartition(b")")[2].split()
        return ("proc", float(fields[19]))
    except (OSError, IndexError, ValueError):
        return None
