def _start_proxy(
    port: int,
    *,
    learn: bool = False,
    memory: bool = False,
    agent_type: str = "unknown",
    code_graph: bool = False,
    backend: str | None = None,
    anyllm_provider: str | None = None,
    region: str | None = None,
    openai_api_url: str | None = None,
    anthropic_api_url: str | None = None,
    vertex_api_url: str | None = None,
    clear_vertex_api_url: bool = False,
    copilot_api_token: str | None = None,
) -> subprocess.Popen:
    """Start Headroom proxy as a background subprocess.

    Stdout and stderr are written to a dedicated sibling file, usually
    `~/.headroom/logs/proxy-stdio.log`, to avoid pipe deadlock risk without
    competing with the rotating `proxy.log` runtime log.
    """
    cmd = [sys.executable, "-m", "headroom.cli", "proxy", "--port", str(port)]

    # Forward HEADROOM_MODE env var so the proxy respects the user's mode choice
    headroom_mode = os.environ.get("HEADROOM_MODE")
    if headroom_mode:
        cmd.extend(["--mode", headroom_mode])

    # Forward --learn flag to proxy subprocess
    if learn:
        cmd.append("--learn")

    # Forward --memory flag to proxy subprocess
    if memory:
        cmd.append("--memory")

    # Forward --code-graph flag to proxy subprocess (live file watcher)
    if code_graph:
        cmd.append("--code-graph")

    # Forward backend configuration to proxy subprocess
    _backend = backend or os.environ.get("HEADROOM_BACKEND")
    if _backend:
        cmd.extend(["--backend", _backend])

    _anyllm = anyllm_provider or os.environ.get("HEADROOM_ANYLLM_PROVIDER")
    if _anyllm:
        cmd.extend(["--anyllm-provider", _anyllm])

    _region = region or os.environ.get("HEADROOM_REGION")
    if _region:
        cmd.extend(["--region", _region])

    if openai_api_url:
        cmd.extend(["--openai-api-url", openai_api_url])

    if anthropic_api_url:
        cmd.extend(["--anthropic-api-url", anthropic_api_url])

    if vertex_api_url:
        cmd.extend(["--vertex-api-url", vertex_api_url])

    timeout_seconds = _resolve_wrap_proxy_timeout_seconds()
    log_path = _get_log_path()
    stdio_log_path = _get_proxy_stdio_log_path()
    stdio_log_file = open(stdio_log_path, "a", encoding="utf-8")  # noqa: SIM115

    # Ensure proxy subprocess uses UTF-8 (Windows defaults to cp1252)
    proxy_env = os.environ.copy()
    proxy_env["PYTHONIOENCODING"] = "utf-8"
    # Vertex AI RST_STREAMs HTTP/2 connections (error_code:2). Force HTTP/1.1
    # when wrapping a Vertex-mode client so upstream requests succeed.
    if os.environ.get("CLAUDE_CODE_USE_VERTEX") or os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID"):
        proxy_env.setdefault("HEADROOM_HTTP2", "false")
    # Tell the proxy which agent is being wrapped (for traffic learning output)
    if agent_type != "unknown":
        proxy_env["HEADROOM_AGENT_TYPE"] = agent_type
        proxy_env.setdefault("HEADROOM_STACK", f"wrap_{agent_type}")
    savings_profile = _wrap_agent_savings_profile(agent_type)
    if savings_profile is not None:
        apply_agent_savings_env_defaults(proxy_env, savings_profile)
    if openai_api_url:
        proxy_env["OPENAI_TARGET_API_URL"] = openai_api_url
    if anthropic_api_url:
        proxy_env["ANTHROPIC_TARGET_API_URL"] = anthropic_api_url
    if clear_vertex_api_url:
        proxy_env.pop("VERTEX_TARGET_API_URL", None)
    if vertex_api_url:
        proxy_env["VERTEX_TARGET_API_URL"] = vertex_api_url
    # Pin the wrapper-validated Copilot token for this proxy instance only.
    # Injected into the subprocess env here (not the parent's os.environ) so it
    # never leaks into shared state. The proxy's CopilotTokenProvider honours
    # GITHUB_COPILOT_API_TOKEN directly, making upstream auth deterministic.
    if copilot_api_token:
        proxy_env["GITHUB_COPILOT_API_TOKEN"] = copilot_api_token
        if openai_api_url:
            proxy_env["GITHUB_COPILOT_API_URL"] = openai_api_url

    # Detach the proxy from the launching console on Windows so an ungraceful
    # close of the owning agent (closing the terminal window, taskkill, or a
    # crash) cannot tree-kill the shared proxy out from under other live
    # clients. Without this the proxy stays in the owner's console + Job
    # object; closing that window terminates the whole tree, bypassing the
    # marker-based reference counting in ``_make_cleanup`` and breaking every
    # other ``headroom wrap`` instance routed through the same port.
    #   CREATE_NO_WINDOW         — give the proxy its OWN, invisible console.
    #                              A separate console means the parent's
    #                              CTRL_CLOSE_EVENT never reaches it, and no
    #                              stray console window pops up. DETACHED_PROCESS
    #                              also isolates the console, but for a console
    #                              subsystem exe (python.exe) it leaves the proxy
    #                              consoleless and Windows surfaces a visible
    #                              console window — closing that window killed
    #                              the proxy, defeating the whole point.
    #   CREATE_NEW_PROCESS_GROUP — isolate from the parent's Ctrl-C
    #   CREATE_BREAKAWAY_FROM_JOB— survive Job kill-on-close (Windows Terminal,
    #                              VS Code integrated terminal, conhost)
    # CREATE_NO_WINDOW / DETACHED_PROCESS / CREATE_NEW_CONSOLE are mutually
    # exclusive — pick exactly one. On POSIX, ``start_new_session`` already
    # detaches via setsid(). ``sys.platform == "win32"`` (not ``os.name ==
    # "nt"``) so mypy narrows the platform and resolves the Windows-only
    # ``subprocess`` constants below.
    _CREATE_BREAKAWAY_FROM_JOB = 0x01000000
    creationflags = 0
    if sys.platform == "win32":
        creationflags = (
            subprocess.CREATE_NO_WINDOW
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | _CREATE_BREAKAWAY_FROM_JOB
        )

    popen_kwargs: dict[str, Any] = {
        "stdout": stdio_log_file,
        "stderr": stdio_log_file,
        "env": proxy_env,
        "start_new_session": os.name == "posix",
        "creationflags": creationflags,
    }
    # Close the parent's copy of the stdio log handle on every exit path,
    # including when BOTH spawn attempts raise. The child keeps its own
    # inherited duplicate, so closing here never starves the proxy's logging.
    try:
        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)
        except OSError:
            # The launcher's Job object forbids breakaway. Retry without that flag;
            # CREATE_NO_WINDOW still spares the proxy from console-close events.
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = creationflags & ~_CREATE_BREAKAWAY_FROM_JOB
            proc = subprocess.Popen(cmd, **popen_kwargs)

        # Wait for proxy to be ready.
        # ML components (Kompress, Magika, Tree-sitter) load synchronously before
        # uvicorn binds the port. On slower machines this can take 20-30 seconds.
        for _i in range(timeout_seconds):
            time.sleep(1)
            if _check_proxy(port):
                click.echo(f"  Logs: {log_path}")
                return proc
            # Check if process died
            if proc.poll() is not None:
                # Read last few lines of log for error context
                try:
                    tail = _read_text(stdio_log_path)[-500:]
                except Exception:
                    tail = "(no log output)"
                raise RuntimeError(f"Proxy exited with code {proc.returncode}: {tail}")

        proc.kill()
        raise RuntimeError(
            f"Proxy failed to start on port {port} within {timeout_seconds} seconds. "
            f"Set {_WRAP_PROXY_TIMEOUT_ENV} to a larger number of seconds for slow startup."
        )
    finally:
        stdio_log_file.close()
