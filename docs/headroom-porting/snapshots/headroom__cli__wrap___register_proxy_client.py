def _register_proxy_client(port: int) -> None:
    """Register this wrap process as a live client of the shared proxy.

    Best-effort: a failed write just means our marker is missing, and the
    liveness pruning in :func:`_live_proxy_clients` is the real safety net.
    """
    try:
        payload: dict[str, Any] = {"pid": os.getpid(), "started_at": time.time()}
        ident = _proc_identity(os.getpid())
        if ident is not None:
            payload["start_src"], payload["start_time"] = ident
        _write_text(_client_marker_path(port), json.dumps(payload))
    except OSError:
        pass
