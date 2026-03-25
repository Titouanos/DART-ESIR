"""
DartVision Web Server
FastAPI backend wrapping the existing detector/fusion/game modules.

Features:
  - REST API for game management (setup, throw, undo, next-turn, etc.)
  - WebSocket for real-time game state broadcast
  - MJPEG streaming for each camera
  - Background thread for camera capture + auto-detection
  - Graceful fallback when cameras unavailable
"""

import asyncio
import json
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional, Dict, Set

import cv2
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── Add parent directory so we can import the existing modules ────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import config
from game import GameEngine
from board import compute_score
from detector import DartDetector
from fusion import FusionEngine, CameraConfidence

# ═══════════════════════════════════════════════════════════════════════════════
# Application & CORS
# ═══════════════════════════════════════════════════════════════════════════════

app = FastAPI(title="DartVision Web", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ═══════════════════════════════════════════════════════════════════════════════
# Shared Application State
# ═══════════════════════════════════════════════════════════════════════════════

class AppState:
    def __init__(self):
        # Game
        self.game: Optional[GameEngine] = None
        self.mode: str = "501"
        self.players: List[str] = []

        # Cameras
        self.cam_indexes: List[int] = []
        self.caps: Dict[int, cv2.VideoCapture] = {}
        self.detectors: Dict[int, DartDetector] = {}
        self.cam_homographies: Dict[int, np.ndarray] = {}
        self.cam_positions: Dict[int, int] = {}  # cam_idx -> segment number

        # Latest JPEG frames for streaming
        self.cam_frames: Dict[int, bytes] = {}
        self.frame_lock = threading.Lock()

        # Camera detection states
        self.cam_states: Dict[int, str] = {}

        # Fusion engine
        self.fusion: Optional[FusionEngine] = None

        # WebSocket clients
        self.ws_clients: Set[WebSocket] = set()

        # asyncio event loop (captured at startup)
        self.event_loop: Optional[asyncio.AbstractEventLoop] = None

        # Background camera thread
        self.running = False
        self.camera_thread: Optional[threading.Thread] = None

        # Detection control
        self.detection_paused = False

        # Last throw info for clients that connect late
        self.last_throw_info: Optional[dict] = None


state = AppState()

# ═══════════════════════════════════════════════════════════════════════════════
# Calibration Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def load_calibration() -> Dict[int, dict]:
    """Load calibration.json from project root. Returns {cam_idx: {homography, segment}}.
    Supports both list format (new) and dict-of-dicts format (legacy).
    """
    calib_file = ROOT / config.CALIB_FILE
    if not calib_file.exists():
        return {}
    try:
        with open(calib_file) as f:
            data = json.load(f)

        # Normalise: accept both list and dict formats
        if isinstance(data, list):
            entries = data
        elif isinstance(data, dict):
            entries = list(data.values())
        else:
            return {}

        result = {}
        for cam_data in entries:
            cam_idx = cam_data.get("cam_index")
            if cam_idx is None:
                continue
            H = np.array(cam_data["homography"], dtype=np.float64)
            seg = cam_data.get("cam_position_segment", 20)
            result[int(cam_idx)] = {"homography": H, "segment": seg}
        return result
    except Exception as e:
        print(f"[WARN] Could not load calibration: {e}")
        return {}


def warp_frame(frame: np.ndarray, H: np.ndarray) -> np.ndarray:
    return cv2.warpPerspective(frame, H, (config.WARP_SIZE, config.WARP_SIZE))


def encode_jpeg(frame: np.ndarray, quality: int = 72) -> bytes:
    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes()


def make_placeholder(cam_idx: int) -> bytes:
    """Generate a gray placeholder JPEG for when a camera is offline."""
    img = np.zeros((config.WARP_SIZE, config.WARP_SIZE, 3), dtype=np.uint8)
    img[:] = (30, 30, 30)
    cv2.putText(img, f"CAM {cam_idx}", (280, 370),
                cv2.FONT_HERSHEY_SIMPLEX, 2.2, (90, 90, 90), 4)
    cv2.putText(img, "Hors ligne", (285, 440),
                cv2.FONT_HERSHEY_SIMPLEX, 1.2, (70, 70, 70), 2)
    return encode_jpeg(img)


# ═══════════════════════════════════════════════════════════════════════════════
# Camera Loop (Background Thread)
# ═══════════════════════════════════════════════════════════════════════════════

def camera_loop():
    """Capture frames, run detection, trigger fusion. Runs in a daemon thread."""
    while state.running:
        try:
            _process_cameras()
        except Exception as e:
            print(f"[ERR] Camera loop: {e}")
        time.sleep(1.0 / config.CAM_FPS)


def _process_cameras():
    if state.game is None or state.detection_paused:
        # Still capture frames for streaming even when paused
        for cam_idx in state.cam_indexes:
            cap = state.caps.get(cam_idx)
            if cap and cap.isOpened():
                ret, frame = cap.read()
                if ret:
                    H = state.cam_homographies.get(cam_idx)
                    warped = warp_frame(frame, H) if H is not None else cv2.resize(
                        frame, (config.WARP_SIZE, config.WARP_SIZE))
                    with state.frame_lock:
                        state.cam_frames[cam_idx] = encode_jpeg(warped)
        return

    for cam_idx in state.cam_indexes:
        cap = state.caps.get(cam_idx)
        if cap is None or not cap.isOpened():
            continue

        ret, frame = cap.read()
        if not ret:
            continue

        # Perspective warp
        H = state.cam_homographies.get(cam_idx)
        warped = warp_frame(frame, H) if H is not None else cv2.resize(
            frame, (config.WARP_SIZE, config.WARP_SIZE))

        # Store JPEG for MJPEG streaming
        with state.frame_lock:
            state.cam_frames[cam_idx] = encode_jpeg(warped)

        # Run detector
        detector = state.detectors.get(cam_idx)
        if detector is None:
            continue

        result = detector.process_frame(warped)
        state.cam_states[cam_idx] = result.get("state", "idle")

        if result.get("tip") is not None:
            tip = result["tip"]
            ray = result.get("ray")
            diff_score = result.get("diff_score", 1.0)

            if state.fusion is not None:
                fused = state.fusion.add_detection(cam_idx, tip, diff_score, ray)
                if fused:
                    _handle_fused_detection(fused)
            else:
                # Single camera – score directly
                sd = compute_score(tip[0], tip[1],
                                   config.WARP_CENTER, config.WARP_CENTER,
                                   config.WARP_RADIUS)
                sd.update({
                    "tip_px": list(tip),
                    "fusion_method": "single",
                    "fusion_cams": [cam_idx],
                    "fusion_confidence": 0.8,
                    "cam_id": cam_idx,
                })
                _handle_fused_detection(sd)

    # Fusion timeout flush
    if state.fusion is not None:
        fused = state.fusion.try_fuse()
        if fused:
            _handle_fused_detection(fused)


def _handle_fused_detection(score_data: dict):
    if state.game is None or state.game.game_over:
        return

    game_result = state.game.register_throw(score_data)
    state.last_throw_info = {
        "score_data": score_data,
        "game_result": {k: game_result[k]
                        for k in ["player", "bust", "turn_complete", "game_over", "winner"]},
    }

    msg = _build_detection_msg(score_data, game_result, "auto")
    _queue_broadcast(msg)


def _build_detection_msg(score_data: dict, game_result: dict, source: str) -> dict:
    tip = score_data.get("tip_px")
    if tip is not None:
        tip = [int(x) for x in tip]  # ensure JSON-serialisable

    return {
        "type": "detection",
        "source": source,
        "label": score_data.get("label", "?"),
        "score": score_data.get("score", 0),
        "tip_px": tip,
        "fusion_method": score_data.get("fusion_method", source),
        "fusion_cams": score_data.get("fusion_cams", []),
        "fusion_confidence": score_data.get("fusion_confidence", 1.0),
        "game_result": {
            "player": game_result.get("player", ""),
            "bust": game_result.get("bust", False),
            "turn_complete": game_result.get("turn_complete", False),
            "game_over": game_result.get("game_over", False),
            "winner": game_result.get("winner"),
        },
        "scoreboard": state.game.get_scoreboard() if state.game else [],
        "mode": state.game.mode if state.game else "free",
        "history": state.game.history[-15:] if state.game else [],
        "cam_states": dict(state.cam_states),
        "current_player": state.game.current_player.name if state.game else "",
    }


def _queue_broadcast(msg: dict):
    """Thread-safe: schedule broadcast on the asyncio event loop."""
    if state.event_loop and not state.event_loop.is_closed():
        asyncio.run_coroutine_threadsafe(_broadcast(msg), state.event_loop)


async def _broadcast(msg: dict):
    """Send JSON to every connected WebSocket client."""
    dead = set()
    for ws in list(state.ws_clients):
        try:
            await ws.send_json(msg)
        except Exception:
            dead.add(ws)
    for ws in dead:
        state.ws_clients.discard(ws)


# ═══════════════════════════════════════════════════════════════════════════════
# Pydantic Models
# ═══════════════════════════════════════════════════════════════════════════════

class SetupRequest(BaseModel):
    mode: str = "501"
    players: List[str] = ["Joueur 1", "Joueur 2"]
    cam_indexes: List[int] = []
    cam_positions: List[int] = []


class ThrowRequest(BaseModel):
    label: str
    score: int
    number: int
    multiplier: int
    ring: str


class PauseRequest(BaseModel):
    paused: bool


# ═══════════════════════════════════════════════════════════════════════════════
# REST Endpoints
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/setup")
async def api_setup(req: SetupRequest):
    """Initialize game and cameras. Stops any running camera thread first."""
    # Stop old loop
    if state.running:
        state.running = False
        if state.camera_thread and state.camera_thread.is_alive():
            state.camera_thread.join(timeout=3.0)

    # Release old cameras
    for cap in state.caps.values():
        cap.release()
    state.caps.clear()
    state.detectors.clear()
    state.cam_frames.clear()
    state.cam_states.clear()
    state.cam_indexes.clear()
    state.cam_homographies.clear()
    state.cam_positions.clear()
    state.fusion = None
    state.last_throw_info = None

    # New game engine
    state.mode = req.mode
    state.players = req.players
    state.game = GameEngine(mode=req.mode, player_names=req.players)

    # Load saved calibration
    calib = load_calibration()
    print(f"[INFO] Calibration found for cameras: {list(calib.keys())}")

    # Open cameras
    cam_indexes = req.cam_indexes if req.cam_indexes else list(config.CAM_INDEXES)
    opened = []
    for i, cam_idx in enumerate(cam_indexes):
        # Try DirectShow on Windows first (faster init)
        cap = cv2.VideoCapture(cam_idx, cv2.CAP_DSHOW if sys.platform == "win32" else 0)
        if not cap.isOpened():
            cap = cv2.VideoCapture(cam_idx)
        if cap.isOpened():
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, config.CAM_WIDTH)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, config.CAM_HEIGHT)
            cap.set(cv2.CAP_PROP_FPS, config.CAM_FPS)
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            state.caps[cam_idx] = cap

            det = DartDetector(i)  # internal index for fusion
            state.detectors[cam_idx] = det

            if cam_idx in calib:
                state.cam_homographies[cam_idx] = calib[cam_idx]["homography"]
                seg = calib[cam_idx]["segment"]
                print(f"[INFO] Camera {cam_idx}: calibration loaded, segment {seg}")
            else:
                seg = (req.cam_positions[i] if i < len(req.cam_positions)
                       else (config.CAM_POSITIONS[i]
                             if i < len(config.CAM_POSITIONS) else 20))
                print(f"[INFO] Camera {cam_idx}: no calibration, using segment {seg}")

            state.cam_positions[cam_idx] = seg
            state.cam_states[cam_idx] = "idle"
            # Pre-fill placeholder so stream starts immediately
            state.cam_frames[cam_idx] = make_placeholder(cam_idx)
            opened.append(cam_idx)
        else:
            print(f"[WARN] Camera {cam_idx} could not be opened")

    state.cam_indexes = opened

    # Build fusion engine if >1 camera
    if len(opened) > 1:
        cam_confidences = [
            CameraConfidence(state.cam_positions.get(idx, 20))
            for idx in opened
        ]
        state.fusion = FusionEngine(cam_confidences)
        print(f"[INFO] Fusion engine active for cameras {opened}")

    # Start camera thread
    state.running = True
    state.detection_paused = False
    state.camera_thread = threading.Thread(target=camera_loop, daemon=True, name="camera-loop")
    state.camera_thread.start()

    return {
        "ok": True,
        "mode": req.mode,
        "players": req.players,
        "cameras": opened,
        "scoreboard": state.game.get_scoreboard(),
    }


@app.get("/api/state")
async def api_state():
    """Return full current game + camera state."""
    if state.game is None:
        return {"game": None}
    return {
        "game": {
            "mode": state.game.mode,
            "game_over": state.game.game_over,
            "winner": state.game.winner,
            "current_player": state.game.current_player.name,
            "scoreboard": state.game.get_scoreboard(),
            "history": state.game.history[-20:],
        },
        "cameras": {
            str(idx): {
                "state": state.cam_states.get(idx, "unknown"),
                "has_calibration": idx in state.cam_homographies,
                "segment": state.cam_positions.get(idx, 0),
            }
            for idx in state.cam_indexes
        },
        "detection_paused": state.detection_paused,
        "last_throw": state.last_throw_info,
    }


@app.post("/api/throw")
async def api_throw(req: ThrowRequest):
    """Register a manually-entered throw (fallback scoring)."""
    if state.game is None:
        raise HTTPException(400, "Aucune partie en cours")
    if state.game.game_over:
        raise HTTPException(400, "La partie est terminée")

    score_data = {
        "label": req.label,
        "score": req.score,
        "number": req.number,
        "multiplier": req.multiplier,
        "ring": req.ring,
        "tip_px": None,
        "fusion_method": "manual",
        "fusion_cams": [],
        "fusion_confidence": 1.0,
    }

    game_result = state.game.register_throw(score_data)
    state.last_throw_info = {
        "score_data": score_data,
        "game_result": {k: game_result[k]
                        for k in ["player", "bust", "turn_complete", "game_over", "winner"]},
    }

    msg = _build_detection_msg(score_data, game_result, "manual")
    await _broadcast(msg)
    return {"ok": True, **msg}


@app.post("/api/undo")
async def api_undo():
    """Undo the last registered throw."""
    if state.game is None:
        raise HTTPException(400, "Aucune partie en cours")

    ok = state.game.undo_last_throw()

    # Also trim detector history
    for detector in state.detectors.values():
        if detector.all_detections:
            detector.all_detections.pop()

    msg = {
        "type": "undo",
        "ok": ok,
        "scoreboard": state.game.get_scoreboard(),
        "history": state.game.history[-15:],
        "mode": state.game.mode,
        "current_player": state.game.current_player.name,
    }
    await _broadcast(msg)
    return msg


@app.post("/api/next-turn")
async def api_next_turn():
    """Force end of current player's turn (dart fell out, etc.)."""
    if state.game is None:
        raise HTTPException(400, "Aucune partie en cours")

    state.game.force_next_turn()

    # Clear detector tips for the new turn
    for detector in state.detectors.values():
        detector.all_detections.clear()

    msg = {
        "type": "next_turn",
        "scoreboard": state.game.get_scoreboard(),
        "history": state.game.history[-15:],
        "mode": state.game.mode,
        "current_player": state.game.current_player.name,
    }
    await _broadcast(msg)
    return msg


@app.post("/api/reference")
async def api_reference_all():
    """Capture current frame as new reference for ALL cameras simultaneously."""
    captured = []
    for cam_idx, cap in state.caps.items():
        if not cap.isOpened():
            continue
        ret, frame = cap.read()
        if not ret:
            continue
        H = state.cam_homographies.get(cam_idx)
        warped = (warp_frame(frame, H) if H is not None
                  else cv2.resize(frame, (config.WARP_SIZE, config.WARP_SIZE)))
        det = state.detectors.get(cam_idx)
        if det:
            det.set_reference(warped)
            captured.append(cam_idx)

    await _broadcast({"type": "reference_captured", "cameras": captured})
    return {"ok": True, "captured": captured}


@app.post("/api/reference/{cam_idx}")
async def api_reference_cam(cam_idx: int):
    """Capture reference frame for a specific camera."""
    cap = state.caps.get(cam_idx)
    det = state.detectors.get(cam_idx)
    if cap is None or not cap.isOpened():
        raise HTTPException(404, f"Caméra {cam_idx} non disponible")

    ret, frame = cap.read()
    if not ret:
        raise HTTPException(500, "Impossible de capturer une frame")

    H = state.cam_homographies.get(cam_idx)
    warped = (warp_frame(frame, H) if H is not None
              else cv2.resize(frame, (config.WARP_SIZE, config.WARP_SIZE)))
    if det:
        det.set_reference(warped)

    await _broadcast({"type": "reference_captured", "cameras": [cam_idx]})
    return {"ok": True, "cam_idx": cam_idx}


@app.post("/api/pause")
async def api_pause(req: PauseRequest):
    """Pause or resume auto-detection."""
    state.detection_paused = req.paused
    await _broadcast({"type": "detection_paused", "paused": req.paused})
    return {"ok": True, "paused": req.paused}


# ═══════════════════════════════════════════════════════════════════════════════
# MJPEG Camera Streaming
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/camera/{cam_idx}")
async def camera_stream(cam_idx: int):
    """Stream a camera as MJPEG (multipart/x-mixed-replace)."""
    placeholder = make_placeholder(cam_idx)

    async def generate():
        while True:
            with state.frame_lock:
                frame_bytes = state.cam_frames.get(cam_idx, placeholder)
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n"
            )
            await asyncio.sleep(1.0 / config.CAM_FPS)

    return StreamingResponse(
        generate(),
        media_type="multipart/x-mixed-replace;boundary=frame",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# WebSocket
# ═══════════════════════════════════════════════════════════════════════════════

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    state.ws_clients.add(ws)

    # Send current state immediately on connect
    if state.game:
        await ws.send_json({
            "type": "state",
            "scoreboard": state.game.get_scoreboard(),
            "mode": state.game.mode,
            "game_over": state.game.game_over,
            "winner": state.game.winner,
            "history": state.game.history[-15:],
            "current_player": state.game.current_player.name,
            "cam_states": dict(state.cam_states),
            "detection_paused": state.detection_paused,
        })

    try:
        while True:
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text("pong")
    except WebSocketDisconnect:
        pass
    finally:
        state.ws_clients.discard(ws)


# ═══════════════════════════════════════════════════════════════════════════════
# Lifecycle
# ═══════════════════════════════════════════════════════════════════════════════

@app.on_event("startup")
async def on_startup():
    state.event_loop = asyncio.get_event_loop()
    print("[INFO] DartVision Web Server ready → http://localhost:8000")


@app.on_event("shutdown")
async def on_shutdown():
    state.running = False
    if state.camera_thread and state.camera_thread.is_alive():
        state.camera_thread.join(timeout=3.0)
    for cap in state.caps.values():
        cap.release()
    print("[INFO] DartVision Web Server stopped.")


# ═══════════════════════════════════════════════════════════════════════════════
# Static Files (must be last – catches all remaining routes)
# ═══════════════════════════════════════════════════════════════════════════════

static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")


# ═══════════════════════════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
