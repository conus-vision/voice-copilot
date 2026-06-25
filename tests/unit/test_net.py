import socket

from voice_copilot.net import free_port


def test_free_port_returns_a_bindable_port() -> None:
    port = free_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))


def test_free_port_returns_different_ports_across_calls() -> None:
    ports = {free_port() for _ in range(5)}
    assert len(ports) > 1
