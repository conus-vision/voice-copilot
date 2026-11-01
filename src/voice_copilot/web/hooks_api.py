"""The endpoints CLI plugins talk to (see `companion/hook_forward.py`)."""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response

from voice_copilot.adapters.hooks import HookAdapter
from voice_copilot.companion.runtime import TOKEN_HEADER


def register_hook_routes(
    app: FastAPI,
    adapter: HookAdapter,
    token: str,
    supervisor_mode: Callable[[], str] = lambda: "off",
    center: Any = None,
) -> None:
    def require_token(request: Request) -> None:
        # Replies are injected into a live agent, so a page in the user's
        # browser must not be able to reach this: the token isn't on any page.
        if not secrets.compare_digest(request.headers.get(TOKEN_HEADER, ""), token):
            raise HTTPException(401, "bad companion token")

    @app.post("/api/switch")
    async def post_switch(request: Request) -> Response:
        require_token(request)
        try:
            asked = json.loads(await request.body())
            by_agent, session_id = bool(asked.get("by_agent")), asked.get("session_id")
        except (ValueError, AttributeError) as e:
            raise HTTPException(400, "switch request is not a JSON object") from e
        if by_agent and supervisor_mode() == "guard":
            # Supervisor+ exists to stop this agent, so the agent can't be the one to end it.
            refused = (
                "Supervisor+ is watching this agent, so only the user can turn it off: `vc off`."
            )
            return Response(content=json.dumps({"refused": refused}), media_type="application/json")
        await adapter.switch_off(by_agent=by_agent, session_id=session_id)
        return Response(status_code=204)

    @app.post("/api/command")
    async def post_command(request: Request) -> Response:
        require_token(request)
        if center is None:
            raise HTTPException(503, "commands aren't available yet")
        try:
            asked = json.loads(await request.body())
            verb, args = str(asked["verb"]), list(asked.get("args") or [])
        except (ValueError, KeyError, AttributeError, TypeError) as e:
            raise HTTPException(400, "command is not a JSON object with a verb") from e
        answer = await center.run(verb, args, asked.get("session_id"))
        return Response(content=json.dumps(answer), media_type="application/json")

    @app.get("/api/hooks/{cli}/mail")
    async def get_mail(cli: str, session_id: str, request: Request) -> Response:
        require_token(request)
        messages = await adapter.take_mail(cli, session_id)
        notices = adapter.take_notices(session_id)
        return Response(
            content=json.dumps({"messages": messages, "notices": notices}),
            media_type="application/json",
        )

    @app.get("/api/hooks/{cli}/working")
    async def get_working(session_id: str, request: Request) -> Response:
        require_token(request)
        working = adapter.is_working(session_id)
        return Response(content=json.dumps({"working": working}), media_type="application/json")

    @app.post("/api/hooks/{cli}")
    async def post_hook(cli: str, request: Request) -> Response:
        require_token(request)
        try:
            payload = json.loads(await request.body())
        except ValueError as e:
            raise HTTPException(400, "hook payload is not JSON") from e
        if not isinstance(payload, dict):
            raise HTTPException(400, "hook payload is not an object")
        reply = await adapter.handle_hook(cli, payload)
        if reply is None:
            return Response(status_code=204)
        return Response(content=json.dumps(reply), media_type="application/json")
