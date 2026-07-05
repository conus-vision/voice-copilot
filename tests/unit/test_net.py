import asyncio
import socket

import pytest

from voice_copilot.net import free_port, wait_for_port


def test_free_port_returns_a_bindable_port() -> None:
    port = free_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))


def test_free_port_returns_different_ports_across_calls() -> None:
    ports = {free_port() for _ in range(5)}
    assert len(ports) > 1


@pytest.mark.asyncio
async def test_wait_for_port_returns_true_once_listener_is_up() -> None:
    host = "127.0.0.1"
    port = free_port(host)

    server = await asyncio.start_server(lambda r, w: None, host, port)
    try:
        assert await wait_for_port(host, port, timeout=2.0) is True
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_wait_for_port_times_out_when_nothing_listens() -> None:
    host = "127.0.0.1"
    port = free_port(host)  # free right now, nobody binds it

    loop = asyncio.get_running_loop()
    start = loop.time()
    assert await wait_for_port(host, port, timeout=0.3, interval=0.02) is False
    # Returned around the deadline, not instantly and not far past it.
    assert 0.25 <= loop.time() - start < 2.0
