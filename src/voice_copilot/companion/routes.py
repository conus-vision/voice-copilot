"""HTTP endpoints plugins and hooks talk to: ``/api/companion/v1``.

* ``POST /hooks/{dialect}``: a hook call from a CLI, as the CLI sent it. The
  reply is in the same dialect, or an empty 200 when there is nothing to say.
  Claude Code posts here itself (``type: "http"`` hooks); other CLIs go
  through the ``voice-copilot-hook`` forwarder.
* ``POST /events``, ``/gate``, ``/commands/next``, ``/commands/result``: the
  RFC 0001 side, for plugins that run inside the CLI (Pi).
* ``GET /status``, ``GET /integrations`` and the install endpoints feed the
  panel's Integrations section.

Hook calls come from local processes without an ``Origin`` header, which the
panel's origin guard lets through; a web page cannot forge them.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from voice_copilot.companion import integrations
from voice_copilot.companion.dialects import DIALECTS
from voice_copilot.companion.hub import CompanionHub

log = logging.getLogger(__name__)

PREFIX = "/api/companion/v1"


def _hub(request: Request) -> CompanionHub:
    hub = getattr(request.app.state, "companion", None)
    if hub is None:
        raise HTTPException(503, "companion hub is not running")
    return hub  # type: ignore[no-any-return]


def _env_value(value: str | None) -> str | None:
    """A header filled from an unset env var arrives empty or as the literal `$NAME`."""
    if not value or value.startswith("$"):
        return None
    return value


async def _json_body(request: Request) -> Any:
    try:
        return await request.json()
    except ValueError:
        return None


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def build_router() -> APIRouter:
    router = APIRouter(prefix=PREFIX)

    @router.post("/hooks/{dialect}")
    async def hook(
        dialect: str,
        request: Request,
        cli: str | None = None,
        event: str | None = None,
        launch: str | None = None,
        mode: str | None = None,
    ) -> Response:
        if dialect not in DIALECTS:
            raise HTTPException(404, f"unknown hook dialect {dialect!r}")
        body = await _json_body(request)
        if not isinstance(body, dict):
            return Response(status_code=200)
        reply = await _hub(request).handle_hook(
            dialect,
            (cli or dialect).lower(),
            body,
            event=event,
            launch=_env_value(launch),
            header_launch=_env_value(request.headers.get("x-voice-copilot-launch")),
            mode=mode,
        )
        if not reply:
            return Response(status_code=200)
        return JSONResponse(reply)

    @router.post("/events", status_code=202)
    async def events(request: Request) -> dict[str, Any]:
        body = await _json_body(request)
        if isinstance(body, list):
            body = {"events": body}
        if not isinstance(body, dict):
            raise HTTPException(400, "expected a JSON object or a list of events")
        batch = body.get("events")
        if batch is None:
            batch = [body]  # a single RFC 0001 event message
        if not isinstance(batch, list):
            raise HTTPException(400, "`events` must be a list")
        first = batch[0] if batch and isinstance(batch[0], dict) else {}
        cli = _str(body.get("cli") or body.get("cli_name") or first.get("cli_name")) or "plugin"
        session = _str(body.get("session_id") or first.get("session_id"))
        accepted = await _hub(request).handle_events(
            cli.lower(),
            session,
            [e for e in batch if isinstance(e, dict)],
            launch=_str(body.get("launch")) or None,
            mode=_str(body.get("mode")) or None,
            cwd=_str(body.get("cwd")) or None,
        )
        return {"ok": accepted}

    @router.post("/gate")
    async def gate(request: Request) -> dict[str, Any]:
        body = await _json_body(request)
        if not isinstance(body, dict):
            return {"decision": "allow"}
        return await _hub(request).gate(
            _str(body.get("cli")).lower() or "plugin",
            _str(body.get("session_id")),
            launch=_str(body.get("launch")) or None,
            mode=_str(body.get("mode")) or None,
        )

    @router.post("/commands/next")
    async def next_command(request: Request) -> dict[str, Any]:
        body = await _json_body(request)
        if not isinstance(body, dict):
            body = {}
        wait = body.get("wait_s")
        command = await _hub(request).next_command(
            _str(body.get("cli")).lower() or "plugin",
            _str(body.get("session_id")),
            wait_s=float(wait) if isinstance(wait, (int, float)) and wait > 0 else 25.0,
            launch=_str(body.get("launch")) or None,
            mode=_str(body.get("mode")) or None,
        )
        return command or {}

    @router.post("/commands/result")
    async def command_result(request: Request) -> dict[str, Any]:
        body = await _json_body(request)
        if isinstance(body, dict):
            await _hub(request).command_result(
                _str(body.get("cli")).lower() or "plugin", _str(body.get("session_id")), body
            )
        return {"ok": True}

    @router.get("/status")
    async def status(request: Request) -> dict[str, Any]:
        return _hub(request).status()

    @router.get("/integrations")
    async def list_integrations(request: Request) -> dict[str, Any]:
        hub = _hub(request)
        return {
            "integrations": integrations.describe_all(port=_panel_port(request)),
            "status": hub.status(),
        }

    @router.post("/integrations/{cli}/install")
    async def install(cli: str, request: Request) -> dict[str, Any]:
        return _run_installer(cli, request, remove=False)

    @router.post("/integrations/{cli}/uninstall")
    async def uninstall(cli: str, request: Request) -> dict[str, Any]:
        return _run_installer(cli, request, remove=True)

    return router


def _panel_port(request: Request) -> int:
    port = getattr(request.app.state, "panel_port", None)
    if isinstance(port, int):
        return port
    return request.url.port or integrations.DEFAULT_PORT


def _run_installer(cli: str, request: Request, *, remove: bool) -> dict[str, Any]:
    try:
        integration = integrations.get(cli)
    except KeyError:
        raise HTTPException(404, f"no integration for {cli!r}") from None
    try:
        if remove:
            report = integration.uninstall()
        else:
            report = integration.install(port=_panel_port(request))
    except integrations.IntegrationError as e:
        raise HTTPException(400, str(e)) from e
    log.info("companion: %s %s: %s", "uninstalled" if remove else "installed", cli, report)
    return {
        "ok": True,
        "report": report,
        "integration": integration.describe(port=_panel_port(request)),
    }
