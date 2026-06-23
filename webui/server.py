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
import base64
import json
import logging
import math
import sys
import threading
from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Le serveur doit pouvoir importer `config` du projet racine
sys.path.insert(0, str(Path(__file__).parent.parent))
import config  # noqa: E402

from webui.bridge import Bridge

logger = logging.getLogger("dartvision.server")

_STATIC_DIR = Path(__file__).parent / "static"
_PAGES = {
    "setup": "setup.html",
    "game": "game.html",
    "calibration": "calibration.html",
    "end": "end.html",
}


# ─────────────────────────────────────────────────────────────────────
# Pydantic models — DOIVENT être au niveau module (pas dans build_app)
# sinon FastAPI ne peut pas introspecter la classe pour decider que c'est
# un body JSON, et il tente de la passer en query param → erreur
# "field required: req".
# ─────────────────────────────────────────────────────────────────────
class CalibComputeRequest(BaseModel):
    slot: str                   # "A" / "B" / "C"
    points: List[List[float]]   # 4 × [x, y] dans la coord du raw frame
    display_w: float
    display_h: float
    raw_w: int = config.CAM_WIDTH
    raw_h: int = config.CAM_HEIGHT


class CalibSaveRequest(BaseModel):
    slot: str
    homography: List[List[float]]
    cam_position_segment: int
    points: List[List[float]]


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

    def _make_mjpeg_response(slot: str, getter, fps: float = 15.0) -> StreamingResponse:
        """Factory pour les flux MJPEG (clean ou debug).

        Le LAN est un hotspot téléphone : la bande passante est LA ressource
        rare (3 flux simultanés dans l'overlay debug). Deux économies :
          - fps paramétrable par type de flux ;
          - dédup : si la frame n'a pas changé depuis le dernier envoi
            (même objet bytes côté bridge), on dort sans renvoyer.
        """
        async def generator():
            placeholder = _placeholder_jpeg(slot)
            last_sent = None
            while True:
                frame = getter(slot) or placeholder
                if frame is not last_sent:
                    last_sent = frame
                    yield (
                        BOUNDARY
                        + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(frame)).encode("ascii")
                        + b"\r\n\r\n"
                        + frame
                        + b"\r\n"
                    )
                await asyncio.sleep(1.0 / fps)

        return StreamingResponse(
            generator(),
            media_type="multipart/x-mixed-replace; boundary=dartvision",
        )

    @app.get("/api/cam/{slot}/mjpeg")
    async def cam_mjpeg(slot: str) -> StreamingResponse:
        return _make_mjpeg_response(slot.upper(), bridge.get_frame, fps=15.0)

    @app.get("/api/cam/{slot}/debug.mjpeg")
    async def cam_mjpeg_debug(slot: str) -> StreamingResponse:
        """Flux annoté (tip détecté, contour, ray, état). Utile pour debug
        quand la détection paraît imprécise — visualiser ce que le pipeline
        voit AVANT que la fusion ne donne un résultat."""
        return _make_mjpeg_response(slot.upper(), bridge.get_frame_debug, fps=12.0)

    @app.get("/api/cam/{slot}/raw.mjpeg")
    async def cam_mjpeg_raw(slot: str) -> StreamingResponse:
        """Flux RAW (pré-warp, taille native cam). Utilisé par le flow de
        recalibration web : l'utilisateur clique sur les 4 wires du board
        dans l'image native, on calcule l'homographie depuis ces points.
        Full-res obligatoire (précision du clic), mais 8 fps suffisent
        pour une scène statique."""
        return _make_mjpeg_response(slot.upper(), bridge.get_frame_raw, fps=8.0)

    # --- Calibration web (4-points clic per cam) -----------------------
    # 4 points cibles dans l'image WARPÉE 800×800. L'ordre est canonique :
    #   [0] 20 top    → (cx, cy − r)
    #   [1] 6  right  → (cx + r, cy)
    #   [2] 3  bottom → (cx, cy + r)
    #   [3] 11 left   → (cx − r, cy)
    _CALIB_DST_PTS = np.float32([
        [config.WARP_CENTER, config.WARP_CENTER - config.WARP_RADIUS],
        [config.WARP_CENTER + config.WARP_RADIUS, config.WARP_CENTER],
        [config.WARP_CENTER, config.WARP_CENTER + config.WARP_RADIUS],
        [config.WARP_CENTER - config.WARP_RADIUS, config.WARP_CENTER],
    ])
    _FACE_SEGMENTS = [3, 11, 20, 6]
    _CALIB_FILE = Path(__file__).parent.parent / config.CALIB_FILE

    @app.post("/api/calibration/compute")
    async def calib_compute(req: CalibComputeRequest):
        """Reçoit 4 points cliqués sur le flux RAW, calcule l'homographie,
        retourne une preview JPEG (base64) de la frame warpée pour validation
        utilisateur avant sauvegarde."""
        if len(req.points) != 4:
            raise HTTPException(400, "Exactement 4 points requis (20, 6, 3, 11)")

        # Rescale display → raw pixel space
        sx = req.raw_w / req.display_w if req.display_w > 0 else 1.0
        sy = req.raw_h / req.display_h if req.display_h > 0 else 1.0
        src_pts = np.float32([[p[0] * sx, p[1] * sy] for p in req.points])

        H, _ = cv2.findHomography(src_pts, _CALIB_DST_PTS)
        if H is None:
            raise HTTPException(500, "Calcul d'homographie échoué — vérifie les 4 points")

        # Auto-détection du segment "face cam" : on prend le plus grand côté
        # du quad cliqué, qui correspond au segment perpendiculaire à l'axe
        # optique → côté du board qui est "le plus large" dans l'image.
        dists = [
            math.dist(req.points[i], req.points[(i + 1) % 4])
            for i in range(4)
        ]
        face_seg = _FACE_SEGMENTS[dists.index(max(dists))]

        # Génère preview warpé à partir de la frame RAW courante
        slot = req.slot.upper()
        raw_jpeg = bridge.get_frame_raw(slot)
        if not raw_jpeg:
            raise HTTPException(404, f"Pas de frame RAW disponible pour slot {slot}")
        arr = np.frombuffer(raw_jpeg, np.uint8)
        raw_frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if raw_frame is None:
            raise HTTPException(500, "Frame RAW corrompue")
        warped = cv2.warpPerspective(raw_frame, H,
                                      (config.WARP_SIZE, config.WARP_SIZE))
        # Dessine la cible attendue par-dessus pour vérif visuelle
        cx, cy, r = config.WARP_CENTER, config.WARP_CENTER, config.WARP_RADIUS
        cv2.circle(warped, (cx, cy), r,             (0, 255, 255), 2)
        cv2.circle(warped, (cx, cy), int(r * 0.63), (0, 255, 255), 1)
        cv2.circle(warped, (cx, cy), int(r * 0.094), (0, 255, 255), 1)
        cv2.drawMarker(warped, (cx, cy), (0, 255, 255), cv2.MARKER_CROSS, 18, 2)
        ok, preview_jpg = cv2.imencode(".jpg", warped,
                                        [int(cv2.IMWRITE_JPEG_QUALITY), 82])
        if not ok:
            raise HTTPException(500, "Encodage preview échoué")
        preview_b64 = base64.b64encode(preview_jpg.tobytes()).decode("ascii")

        return {
            "ok": True,
            "homography": H.tolist(),
            "cam_position_segment": face_seg,
            "points": req.points,        # echo (utile pour /save)
            "preview_b64": preview_b64,  # "data:image/jpeg;base64," à préfixer côté front
        }

    @app.post("/api/calibration/save")
    async def calib_save(req: CalibSaveRequest):
        """Sauvegarde le résultat de /compute dans calibration.json + reload
        à chaud côté main.py (via Controller.reload_calibration)."""
        # Trouve le cam_index physique correspondant au slot
        slot = req.slot.upper()
        spec = next((s for s in config.CAM_SLOTS
                     if str(s.get("slot", "")).upper() == slot), None)
        if not spec:
            raise HTTPException(404, f"Slot {slot} inconnu dans CAM_SLOTS")
        cam_index = spec["index"]

        # Read existing JSON, remove old entry pour ce cam_index, append nouveau
        data = []
        if _CALIB_FILE.exists():
            try:
                with open(_CALIB_FILE) as f:
                    raw = json.load(f)
                data = raw if isinstance(raw, list) else []
            except Exception as e:
                logger.warning("calibration.json corrompu (%s), on repart vierge", e)
                data = []
        data = [c for c in data if c.get("cam_index") != cam_index]
        data.append({
            "cam_index": cam_index,
            "points": req.points,
            "homography": req.homography,
            "cam_position_segment": req.cam_position_segment,
        })
        with open(_CALIB_FILE, "w") as f:
            json.dump(data, f, indent=2)

        # Reload à chaud côté main.py
        ctrl = bridge.controller
        reloaded = False
        if ctrl is not None and hasattr(ctrl, "reload_calibration"):
            try:
                reloaded = bool(ctrl.reload_calibration())
            except Exception as e:
                logger.exception("reload_calibration crashed: %s", e)

        return {
            "ok": True,
            "slot": slot,
            "cam_index": cam_index,
            "saved_entries": len(data),
            "reloaded": reloaded,
            "msg": "Pense à recapturer la référence (bouton 📸) après recalibration.",
        }

    @app.get("/api/calibration/status")
    async def calib_status():
        """État courant de calibration.json (utile pour l'UI : quelles cams
        sont déjà calibrées vs vierges)."""
        if not _CALIB_FILE.exists():
            return {"has_file": False, "entries": []}
        try:
            with open(_CALIB_FILE) as f:
                data = json.load(f)
        except Exception:
            return {"has_file": True, "entries": [], "error": "corrupted"}
        # Map cam_index → slot pour l'UI
        idx_to_slot = {s["index"]: s.get("slot", "?")
                       for s in config.CAM_SLOTS}
        return {
            "has_file": True,
            "entries": [
                {
                    "cam_index": c.get("cam_index"),
                    "slot": idx_to_slot.get(c.get("cam_index"), "?"),
                    "segment": c.get("cam_position_segment"),
                }
                for c in data
            ],
        }

    @app.get("/api/health")
    async def health():
        """Bilan santé en un appel HTTP (sans WebSocket) : sert au check
        pré-demo « le Pi est-il prêt ? ». Résume le dernier system_status
        connu du bridge + un verdict global facile à tester depuis un script.

        Le simple fait que cette route réponde 200 prouve déjà que le
        serveur web tourne ; les champs disent si les caméras et la
        référence sont prêtes pour scorer."""
        status = getattr(bridge, "_last_status", None) or {}
        cams = status.get("cams", [])
        cams_ok = sum(1 for c in cams if c.get("ok"))
        ref_ok = bool(status.get("reference", {}).get("ok"))
        calib_ok = bool(status.get("calibration", {}).get("ok"))
        # « prêt à jouer » = serveur up + toutes cams vivantes + ref capturée
        ready = bool(cams) and cams_ok == len(cams) and ref_ok and calib_ok
        return {
            "ok": True,                      # la route répond → serveur vivant
            "ready": ready,
            "cams_ok": cams_ok,
            "cams_total": len(cams),
            "cams": [{"id": c.get("id"), "ok": c.get("ok"),
                      "seg": c.get("seg")} for c in cams],
            "reference_ok": ref_ok,
            "reference_at": status.get("reference", {}).get("captured_at"),
            "calibration_ok": calib_ok,
            "game_state": status.get("game_state"),
            "has_status": bool(status),
        }

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
