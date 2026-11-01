"""Who may use the panel besides the browser on this computer.

The Host and Origin checks in `guard.py` stop web pages. They do not stop
another device: with the panel on a network address (``VOICE_COPILOT_HOST=0.0.0.0``
to open it from a phone), anyone on that network could launch a terminal,
read the trace or replace the API keys. So a request from another address
must carry the panel's token: once as ``?token=`` in the link printed at
start, then as a cookie. Requests from this computer need none, and the CLI
plugins and hooks keep working unchanged.

Setting ``VOICE_COPILOT_TOKEN`` makes every request show a token, from this
computer too. That is for a machine shared with other people, whose processes
reach 127.0.0.1 as well. The hook forwarder and the plugins send the value of
the same variable.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import secrets
import socket
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import parse_qs

log = logging.getLogger(__name__)

TOKEN_ENV = "VOICE_COPILOT_TOKEN"
#: Header the hook forwarder and the plugins send the token in.
TOKEN_HEADER = "x-voice-copilot-token"
COOKIE = "voice_copilot_token"
#: Browsers cap cookie lifetime at 400 days.
COOKIE_MAX_AGE_S = 400 * 24 * 3600
_WILDCARD_HOSTS = frozenset({"", "0.0.0.0", "::"})


def is_local_client(host: str | None) -> bool:
    """Whether a request comes from this computer.

    No address (a Unix socket) or a name that is no IP address (Starlette's
    test client) is no network peer either.
    """
    if not host:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def listens_beyond_this_computer(host: str) -> bool:
    if host in _WILDCARD_HOSTS:
        return True
    if host == "localhost":
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return True  # a host name the network resolves to this machine


def default_token_file() -> Path:
    from voice_copilot.core.config import config_path

    return config_path().parent / "panel-token"


class PanelAccess:
    """The panel's token and when a request has to show it."""

    def __init__(
        self,
        token: str | None = None,
        *,
        everywhere: bool | None = None,
        token_file: Path | None = None,
    ) -> None:
        if token is None:
            token = os.environ.get(TOKEN_ENV, "").strip() or None
        #: A token the user chose applies to this computer as well.
        self.everywhere = bool(token) if everywhere is None else everywhere
        self._token = token
        self._token_file = token_file

    @property
    def token(self) -> str:
        # Made on first use: a panel only this computer talks to never needs one.
        if self._token is None:
            self._token = _stored_token(self._token_file or default_token_file())
        return self._token

    def required(self, client_host: str | None) -> bool:
        return self.everywhere or not is_local_client(client_host)

    def accepts(self, candidate: str | None) -> bool:
        if not candidate:
            return False
        return hmac.compare_digest(candidate.encode("utf-8"), self.token.encode("utf-8"))

    def presented(self, headers: Mapping[str, str]) -> str | None:
        """The token a request carries in a header or the cookie; `headers` are lower-case."""
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        if headers.get(TOKEN_HEADER):
            return headers[TOKEN_HEADER].strip()
        for part in headers.get("cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                return value.strip()
        return None

    @staticmethod
    def from_link(query_string: str) -> str | None:
        values = parse_qs(query_string).get("token")
        return values[0] if values else None

    def cookie(self) -> str:
        return f"{COOKIE}={self.token}; Path=/; Max-Age={COOKIE_MAX_AGE_S}; HttpOnly; SameSite=Lax"

    def browser_url(self, host: str, port: int) -> str:
        """The link to open the panel on this computer."""
        shown = "127.0.0.1" if host in _WILDCARD_HOSTS else host
        if ":" in shown:
            shown = f"[{shown}]"
        url = f"http://{shown}:{port}/"
        return f"{url}?token={self.token}" if self.everywhere else url

    def network_url(self, host: str, port: int) -> str | None:
        """The link for another device, when the panel listens beyond this computer."""
        if not listens_beyond_this_computer(host):
            return None
        address = _lan_address() if host in _WILDCARD_HOSTS else host
        if address is None:
            return None
        if ":" in address:
            address = f"[{address}]"
        return f"http://{address}:{port}/?token={self.token}"


def _stored_token(path: Path) -> str:
    """This install's panel token, kept so a phone's link survives restarts."""
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except OSError:
        existing = ""
    if existing:
        return existing
    token = secrets.token_urlsafe(24)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token)
    except OSError as e:
        log.warning("could not save the panel token to %s (%s); it lasts this run only", path, e)
    return token


def _lan_address() -> str | None:
    """This computer's address on its main network, if it has one."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            # Connecting a UDP socket sends nothing; it only picks the route.
            probe.connect(("192.0.2.1", 9))
            address = str(probe.getsockname()[0])
    except OSError:
        return None
    return None if ipaddress.ip_address(address).is_loopback else address
