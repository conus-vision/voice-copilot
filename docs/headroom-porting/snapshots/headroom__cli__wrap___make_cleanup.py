def _make_cleanup(proxy_proc_holder: list, port: int = 8787) -> Any:
    """Create a cleanup function that terminates the proxy on exit.

    Only kills the proxy when no other live headroom-wrapped clients remain,
    tracked via per-PID marker files in ``paths.proxy_clients_dir(port)``.
    """

    def _other_clients_exist() -> bool:
        # Reference-count from marker files, not argv scans. Wrapped clients
        # carry the proxy URL in ANTHROPIC_BASE_URL/OPENAI_BASE_URL (env, not
        # argv), so `pgrep -f` could never see them — and it matched unrelated
        # processes by substring. Markers are exact and OS-portable.
        return len(_live_proxy_clients(port, exclude_self=True)) > 0

    def cleanup(signum: int | None = None, frame: Any = None) -> None:
        # Drop our own marker first so the count reflects the post-exit state;
        # also covers the signal path, where the `finally` block may not run.
        _unregister_proxy_client(port)
        proc = proxy_proc_holder[0] if proxy_proc_holder else None
        if proc and proc.poll() is None:
            if _other_clients_exist():
                # Other clients still using the proxy — leave it running.
                return
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    return cleanup
