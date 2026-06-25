"""Small networking helpers shared by the CLI entrypoints."""

from __future__ import annotations

import socket


def free_port(host: str = "127.0.0.1") -> int:
    """Return a currently-unused TCP port on `host`.

    There's a small window between closing this probe socket and the caller
    binding the real server to the returned port where another process
    could grab it first — acceptable here since `vc` instances are
    short-lived, locally-bound dev tools, not a multi-tenant service.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])
