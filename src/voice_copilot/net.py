"""Small networking helpers shared by the CLI entrypoints."""

from __future__ import annotations

import asyncio
import contextlib
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


async def wait_for_port(
    host: str,
    port: int,
    *,
    timeout: float = 5.0,
    interval: float = 0.05,
) -> bool:
    """Wait until `host:port` accepts a TCP connection, up to `timeout` seconds.

    Returns True once the port is connectable, False if the deadline passes
    first. Used before handing the terminal to a wrapped CLI so its first
    request cannot race ahead of the proxy's bind and slip past unnarrated.

    Cooperative by design: our proxy runs as a task on the *same* event loop,
    so awaiting here yields control and lets uvicorn finish binding — a plain
    ``sleep`` would just guess at how long that takes.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        try:
            # Bound each attempt by the time left: on Windows a connect to a
            # not-yet-listening loopback port can hang for seconds instead of
            # refusing fast, which would otherwise blow past `timeout`.
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=remaining
            )
        except (OSError, TimeoutError):
            pass
        else:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(interval)
