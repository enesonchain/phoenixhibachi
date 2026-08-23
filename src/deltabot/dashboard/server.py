"""Local control-dashboard web server.

Serves the single-file UI plus a small JSON API over the shared
``BotController``. Binds to 127.0.0.1 by default: whoever can reach this
port can pause the bot and close positions, so never expose it publicly
without putting real authentication in front of it.

Routes:
    GET  /             the dashboard UI
    GET  /api/status   full engine status + history ring buffer
    POST /api/control  {"action": "pause" | "resume" | "close" | "clear_halt"}
    POST /api/config   {"entry_apr": "0.12", ...}  (tunable strategy fields)
"""

from __future__ import annotations

import logging
from pathlib import Path

from aiohttp import web

from deltabot.control import TUNABLE_FIELDS, BotController

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

CONTROL_ACTIONS = ("pause", "resume", "close", "clear_halt")


def create_app(controller: BotController) -> web.Application:
    async def index(_request: web.Request) -> web.Response:
        return web.Response(
            text=(STATIC_DIR / "index.html").read_text(),
            content_type="text/html",
        )

    async def status(_request: web.Request) -> web.Response:
        return web.json_response(controller.status_payload())

    async def control(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="body must be JSON")
        action = body.get("action")
        if action not in CONTROL_ACTIONS:
            raise web.HTTPBadRequest(text=f"action must be one of {CONTROL_ACTIONS}")
        if action == "pause":
            controller.paused = True
        elif action == "resume":
            controller.paused = False
        elif action == "close":
            controller.request_close()
        elif action == "clear_halt":
            controller.request_clear_halt()
        log.info("dashboard control: %s", action)
        return web.json_response({"ok": True, "action": action})

    async def config(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="body must be JSON")
        updates = {k: v for k, v in body.items() if k in TUNABLE_FIELDS}
        errors = controller.set_config(updates)
        if errors:
            return web.json_response({"ok": False, "errors": errors}, status=422)
        log.info("dashboard config staged: %s", updates)
        return web.json_response({"ok": True, "staged": sorted(updates)})

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/control", control)
    app.router.add_post("/api/config", config)
    return app


async def start_dashboard(
    controller: BotController, host: str = "127.0.0.1", port: int = 8790
) -> web.AppRunner:
    runner = web.AppRunner(create_app(controller), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("dashboard listening on http://%s:%d", host, port)
    return runner
