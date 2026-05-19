"""
DartVision Web Server
=====================
FastAPI + WebSocket + MJPEG ; tourne dans un thread séparé démarré par main.py.

Endpoints
---------
GET  /                  → setup.html (page d'accueil)
GET  /setup             → setup.html
GET  /game              → game.html
GET  /calibration       → calibration.html
GET  /end               → end.html
GET  /static/<file>     → fichiers statiques (style.css, app.js…)
GET  /api/cam/{slot}/mjpeg  → flux MJPEG d'une cam (slot = A/B/C)
WS   /ws                → canal temps réel bidirectionnel
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from webui.bridge import Bridge

logger = logging.getLogger("dartvision.server")

_STATIC_DIR = Path(__file__).parent / "static"
_PAGES = {
    "setup": "setup.html",
    "game": "game.html",
    "calibration": "calibration.html",
    "end": "end.html",
}


def build_app(bridge: Bridge) -> FastAPI:
    """Construit l'app FastAPI. Le bridge est injecté pour partager l'état avec main.py."""
    app = FastAPI(title="DartVision Web", version="3.0.0")

    # --- Pages HTML ------------------------------------------------------
    def _serve_page(name: str) -> FileResponse:
        path = _STATIC_DIR / _PAGES[name]
        if not path.exists():
            raise HTTPException(404, f"Page absente: {path.name}")
        return FileResponse(str(path), media_type="text/html")

    @app.get("/")
    @app.get("/setup")
    async def page_setup() -> FileResponse:  # noqa: D401
        return _serve_page("setup")

    @app.get("/game")
    async def page_game() -> FileResponse:
        return _serve_page("game")

    @app.get("/calibration")
    async def page_calibration() -> FileResponse:
        return _serve_page("calibration")

    @app.get("/end")
    async def page_end() -> FileResponse:
        return _serve_page("end")

    # --- Statique --------------------------------------------------------
    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # --- MJPEG par slot caméra ------------------------------------------
    BOUNDARY = b"--dartvision"

    @app.get("/api/cam/{slot}/mjpeg")
    async def cam_mjpeg(slot: str) -> StreamingResponse:
        slot = slot.upper()

        async def generator():
            # ~20 fps ; chaque tick on relit le dernier JPEG du bridge.
            placeholder = _placeholder_jpeg(slot)
            while True:
                frame = bridge.get_frame(slot) or placeholder
                yield (
                    BOUNDARY
                    + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(frame)).encode("ascii")
                    + b"\r\n\r\n"
                    + frame
                    + b"\r\n"
                )
                await asyncio.sleep(1.0 / 20.0)

        return StreamingResponse(
            generator(),
            media_type="multipart/x-mixed-replace; boundary=dartvision",
        )

    # --- WebSocket -------------------------------------------------------
    @app.websocket("/ws")
    async def ws_handler(ws: WebSocket) -> None:
        await bridge.register_subscriber(ws)
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await ws.send_text(json.dumps({
                        "type": "error",
                        "payload": {"msg": "JSON invalide"},
                    }))
                    continue
                ack = bridge.handle_command(msg)
                # Ack ciblé : seulement à l'émetteur, pas en broadcast.
                await ws.send_text(json.dumps({
                    "type": "ack",
                    "payload": {"cmd": msg.get("type"), **ack},
                }, separators=(",", ":"), default=str))
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.warning("WS handler crashed: %s", e)
        finally:
            bridge.unregister_subscriber(ws)

    # --- Startup : on récupère la loop pour le bridge --------------------
    @app.on_event("startup")
    async def _on_start() -> None:  # pragma: no cover (testé via curl)
        bridge.set_loop(asyncio.get_running_loop())
        logger.info("DartVision Web Server prêt → http://0.0.0.0:%d", _current_port[0])

    return app


# Placeholder simple (1×1 noir) pour les cams non encore alimentées par main.py.
def _placeholder_jpeg(slot: str) -> bytes:
    return (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n"
        b"\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d"
        b"\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xc0\x00\x0b"
        b"\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05"
        b"\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03"
        b"\x04\x05\x06\x07\x08\t\n\x0b\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03"
        b"\x02\x04\x03\x05\x05\x04\x04\x00\x00\x01}\x01\x02\x03\x00\x04\x11\x05"
        b"\x12!1A\x06\x13Qa\x07\"q\x142\x81\x91\xa1\x08#B\xb1\xc1\x15R\xd1\xf0"
        b"$3br\x82\x09\n\x16\x17\x18\x19\x1a%&'()*456789:CDEFGHIJSTUVWXYZcdef"
        b"ghijstuvwxyz\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97"
        b"\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6"
        b"\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5"
        b"\xd6\xd7\xd8\xd9\xda\xe1\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf1\xf2"
        b"\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xda\x00\x08\x01\x01\x00\x00?\x00"
        b"\xfb\xd0\xff\xd9"
    )


# Mémorisation du port pour le log de startup.
_current_port: list = [8000]


def serve_in_thread(bridge: Bridge, host: str, port: int) -> threading.Thread:
    """Démarre uvicorn dans un thread daemon ; retourne le thread pour join éventuel."""
    _current_port[0] = port
    app = build_app(bridge)
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,    # on limite le bruit ; les hits MJPEG sont nombreux
        loop="asyncio",
        ws="websockets",
    )
    server = uvicorn.Server(config)

    def _run() -> None:
        asyncio.run(server.serve())

    th = threading.Thread(target=_run, daemon=True, name="webui-server")
    th.start()
    return th
