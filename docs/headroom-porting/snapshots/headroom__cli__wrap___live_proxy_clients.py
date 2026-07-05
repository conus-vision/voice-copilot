def _live_proxy_clients(port: int, *, exclude_self: bool = True) -> list[int]:
    """Live wrap-client PIDs for ``port``, pruning stale markers as we go."""
    from headroom import paths as _paths

    d = _paths.proxy_clients_dir(port)
    if not d.exists():
        return []
    me = os.getpid()
    live: list[int] = []
    for marker in d.glob("*.json"):
        try:
            pid = int(marker.stem)
        except ValueError:
            continue
        # Stale if the PID is gone, or recycled by an unrelated process.
        if not _pid_alive(pid) or _marker_pid_reused(marker, pid):
            try:
                marker.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        if not (exclude_self and pid == me):
            live.append(pid)
    return live
