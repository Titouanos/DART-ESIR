#!/usr/bin/env python3
"""
DartVision v3 - Automatic Dart Scoring System
==============================================
Multi-camera (2-4) dart detection with confidence zone fusion.

Usage:
    python main.py --cams 8 6 4 --recalibrate
    python main.py --mode 501 --names Titouan Kevin --cams 8 6 4
    python main.py --cams 8 6 4 --cam-positions 11 18 6

Controls:
    SPACE   Set/reset reference (empty board)
    u       Undo last throw
    n       Force next turn
    c       Recalibrate
    d       Toggle debug overlay
    r       Remove all darts / full reset
    1-3     Toggle individual camera view
    q/ESC   Quit
"""

import argparse
import time
from datetime import datetime
from typing import Optional

import logging
import cv2
import numpy as np

import config
from calibration import Calibrator, calibrate_all_cameras, save_calibrations, load_calibrations
from detector import DartDetector
from board import compute_score, draw_board_overlay
from fusion import FusionEngine, CameraConfidence
from game import GameEngine
from webui.bridge import Bridge
from webui.server import serve_in_thread

logger = logging.getLogger("dartvision.main")


class DartVision:

    def __init__(self, mode="free", num_players=1, player_names=None,
                 recalibrate=False, cam_positions=None, headless=False):
        self.mode = mode
        self.recalibrate = recalibrate
        self.debug_mode = False
        self.running = True
        self.headless = headless
        self.cam_positions_override = cam_positions
        # Timestamp du dernier read OK par cam (alimenté dans la boucle).
        # Permet de détecter un unplug USB : isOpened() reste True après débranchement,
        # mais read() retourne False. On considère une cam KO si pas de read OK > 1s.
        self._last_cam_read: list = []

        names = player_names or [f"Player {i+1}" for i in range(num_players)]
        self.game = GameEngine(mode=mode, player_names=names)

        self.caps = []
        self.calibrators = []
        self.detectors = []
        self.fusion = None
        self.cam_visible = []  # Toggle visibility per cam

        self.last_score = None
        self.last_score_time = 0

        # Clickable buttons config: (label, x, y, w, h, key_equiv, color)
        self.buttons = []

        # --- Bridge web : démarrage du serveur FastAPI dans un thread séparé. ---
        # Le bridge expose un contrat (Controller) pour les actions hors-GameEngine
        # (capture_reference, recalibration, quit). GameEngine notifie via observer.
        self.bridge = Bridge()
        self.bridge.attach_game(self.game)
        self.bridge.attach_controller(self)
        self._web_thread = serve_in_thread(
            self.bridge,
            host=getattr(config, "WEB_HOST", "0.0.0.0"),
            port=getattr(config, "WEB_PORT", 8000),
        )
        self._last_status_emit = 0.0
        self._recalib_requested = False
        self._ref_captured_at: Optional[str] = None

    # -----------------------------------------------------------------
    # INIT
    # -----------------------------------------------------------------
    def init_cameras(self) -> bool:
        """Test that cameras exist (open/close one at a time)."""
        print("\n[INIT] Testing cameras...")
        valid_indexes = []
        for cam_idx in config.CAM_INDEXES:
            cap = cv2.VideoCapture(cam_idx)
            if cap.isOpened():
                w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                print(f"  Camera {cam_idx}: OK ({w}x{h})")
                valid_indexes.append(cam_idx)
                cap.release()
            else:
                print(f"  Camera {cam_idx}: NOT FOUND")

        if not valid_indexes:
            print("ERROR: No cameras found.")
            return False

        config.CAM_INDEXES = valid_indexes
        print(f"  {len(valid_indexes)} camera(s) detected.")
        return True

    # -----------------------------------------------------------------
    # RENDER
    # -----------------------------------------------------------------
    def render_scoreboard(self, canvas, base_x):
        w = config.WINDOW_WIDTH - base_x
        h = config.WINDOW_HEIGHT
        x_off = base_x

        # Title
        mode_text = self.game.mode.upper() if self.game.mode != "free" else "FREE PLAY"
        cv2.putText(canvas, f"DARTVISION v3 - {mode_text}", (x_off+10, 30),
                    cv2.FONT_HERSHEY_DUPLEX, 0.6, config.COLOR_CYAN, 1)

        # Game Over Overlay
        if self.game.game_over:
            # Darken the scoreboard background slightly
            overlay = canvas.copy()
            cv2.rectangle(overlay, (x_off, 0), (config.WINDOW_WIDTH, h), (0, 0, 0), -1)
            cv2.addWeighted(overlay, 0.6, canvas, 0.4, 0, canvas)
            
            # Winner text
            winner_text = f"{self.game.winner} WINS!"
            text_sz, _ = cv2.getTextSize(winner_text, cv2.FONT_HERSHEY_DUPLEX, 1.0, 2)
            tx = x_off + (w - text_sz[0]) // 2
            ty = h // 3
            cv2.putText(canvas, winner_text, (tx, ty),
                        cv2.FONT_HERSHEY_DUPLEX, 1.0, config.COLOR_GREEN, 2)
            
            sub_text = "Press 'r' to restart"
            sub_sz, _ = cv2.getTextSize(sub_text, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
            cv2.putText(canvas, sub_text, (x_off + (w - sub_sz[0]) // 2, ty + 40),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, config.COLOR_WHITE, 1)
            return

        y = 60
        board = self.game.get_scoreboard()
    # -----------------------------------------------------------------
    # CALIBRATION
    # -----------------------------------------------------------------
    def calibrate(self) -> bool:
        if not self.recalibrate:
            loaded = load_calibrations()
            # Restreint et ordonne les calibrators selon config.CAM_INDEXES.
            # Tout calibrator dont cam_index n'est pas dans CAM_INDEXES est
            # ignoré — ça absorbe naturellement les artefacts (metadata nodes,
            # vieilles entrées d'une calibration antérieure sur un autre port
            # USB, etc.) sans dépendre d'une liste statique.
            by_idx = {c.cam_index: c for c in loaded}
            wanted = list(config.CAM_INDEXES)
            ordered = [by_idx[i] for i in wanted if i in by_idx]
            stale = [c.cam_index for c in loaded if c.cam_index not in wanted]
            if stale:
                msg = (f"calibration.json contient des cam_index hors "
                       f"CAM_INDEXES={wanted}, ignorés: {stale}")
                logger.warning(msg)
                print(f"[WARN] {msg}")
            if ordered and len(ordered) == len(wanted):
                self.calibrators = ordered
                self._open_all_cameras()
                self._init_fusion()
                return True
            # Si on arrive ici : calibration incomplète. En mode headless on
            # ne peut PAS lancer le flow interactif → on échoue clean plutôt
            # que de crasher sur Qt.
            if self.headless:
                missing = [i for i in wanted if i not in by_idx]
                msg = (f"Calibration manquante pour cam(s) {missing}. "
                       f"Recalibre via main.py sans --headless.")
                logger.error(msg)
                print(f"[ERROR] {msg}")
                return False

        # Release any open cameras before calibration (USB bandwidth)
        for cap in self.caps:
            cap.release()
        self.caps = []

        result = calibrate_all_cameras(
            None, config.CAM_INDEXES, self.cam_positions_override
        )
        if result is None:
            return False

        self.calibrators = result
        save_calibrations(self.calibrators)

        # Reopen all cameras after calibration
        self._open_all_cameras()
        self._init_fusion()
        return True

    def _open_all_cameras(self):
        """Open all cameras simultaneously for the main loop."""
        self.caps = []
        self.detectors = []
        print("\n[INIT] Opening all cameras...")
        for i, cam_idx in enumerate(config.CAM_INDEXES):
            cap = cv2.VideoCapture(cam_idx)
            if not cap.isOpened():
                print(f"  WARN: Camera {cam_idx} failed to open")
                continue
            # MJPEG uses way less USB bandwidth than raw YUYV
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('M','J','P','G'))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, config.CAM_WIDTH)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, config.CAM_HEIGHT)
            cap.set(cv2.CAP_PROP_FPS, config.CAM_FPS)
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            self.caps.append(cap)
            self.detectors.append(DartDetector(cam_id=i))
            print(f"  Camera {cam_idx}: {w}x{h} MJPEG")
        self.cam_visible = [True] * len(self.caps)
        print(f"  {len(self.caps)} camera(s) ready.")

    # Remarque : `calibrate()` était dupliqué dans le fichier original. La 2e
    # définition (qui n'avait pas le filtre metadata) a été retirée pour ne
    # garder que la version filtrée + ordonnée par CAM_INDEXES.

    def _init_fusion(self):
        """Initialize fusion engine from calibration data."""
        confidences = []
        for i, calib in enumerate(self.calibrators):
            seg = calib.cam_position_segment
            if seg is None:
                # Fallback: use config default or 20
                if i < len(config.CAM_POSITIONS):
                    seg = config.CAM_POSITIONS[i]
                else:
                    seg = 20
            print(f"  Cam {i} confidence zone: segment {seg} "
                  f"(angle {config.SEGMENT_ANGLES.get(seg, 0):.0f} deg)")
            confidences.append(CameraConfidence(seg))
        self.fusion = FusionEngine(confidences)

    # -----------------------------------------------------------------
    # CONTROLLER (consommé par webui.Bridge pour les actions non-game)
    # -----------------------------------------------------------------
    @staticmethod
    def _metadata_nodes() -> set:
        """Indexes V4L2 qui ne sont pas de vraies caméras (metadata libcamera).

        Sur les Pi Bookworm chaque cam USB expose 2 nodes : capture (pair) +
        metadata (impair). On les filtre ici pour ne pas calibrer sur un node
        qui ne renvoie pas d'image utile.
        """
        return {1, 3, 5}

    def start_recalibration(self) -> None:
        """Demande une recalibration. Refuse explicitement en mode headless.

        `calibrate_all_cameras()` (dans calibration.py) est un flow interactif
        avec `cv2.namedWindow` + clics souris pour les 4 points. Sans X server
        on crashe sur "qt.qpa.plugin xcb". Refuser ici avec un message clair
        évite que le bouton "Recalibrer" du web fasse tomber tout le pipeline.
        """
        if self.headless:
            raise RuntimeError(
                "Recalibration interactive non disponible en mode --headless. "
                "Lance main.py sans --headless avec un écran HDMI branché."
            )
        self._recalib_requested = True

    def request_shutdown(self) -> None:
        self.running = False

    # -----------------------------------------------------------------
    # SYSTÈME — snapshot pour push_status
    # -----------------------------------------------------------------
    def _build_system_snapshot(self) -> dict:
        """Construit le dict système attendu par bridge.push_status()."""
        slots = getattr(config, "CAM_SLOTS", [
            {"slot": "A", "index": 0, "master": False},
            {"slot": "B", "index": 2, "master": False},
            {"slot": "C", "index": 4, "master": True},
        ])
        cams_info = []
        now_ts = time.time()
        for spec in slots:
            slot = spec["slot"]
            idx = spec["index"]
            cap_ok = False
            seg = None
            try:
                pos = next((i for i, c in enumerate(self.calibrators)
                            if c.cam_index == idx), None)
                if pos is not None and pos < len(self.caps):
                    # isOpened() reste True même après unplug USB ; on croise avec
                    # le timestamp du dernier read OK (vivant si < 1.5s).
                    if self._last_cam_read and pos < len(self._last_cam_read):
                        cap_ok = (now_ts - self._last_cam_read[pos]) < 1.5
                    else:
                        cap_ok = self.caps[pos].isOpened()
                    seg = self.calibrators[pos].cam_position_segment
            except Exception:
                pass
            cams_info.append({
                "id": slot,
                "ok": cap_ok,
                "fps": config.CAM_FPS,
                "seg": seg,
                "latency_ms": 0,  # placeholder ; sera mesuré plus tard si besoin
                "master": bool(spec.get("master")),
            })
        return {
            "cams": cams_info,
            "calibration": {
                "ok": bool(self.calibrators),
                "residual_mm": 0.0,  # `calibration.py` ne calcule pas encore le résiduel RANSAC
            },
            "reference": {
                "ok": self._ref_captured_at is not None,
                "captured_at": self._ref_captured_at,
            },
            "game_state": "end" if self.game.game_over else "live",
            # Permet au frontend de désactiver les fonctions qui exigent un X server
            # (recalibration interactive notamment).
            "headless": bool(self.headless),
        }

    # -----------------------------------------------------------------
    # REFERENCE CAPTURE
    # -----------------------------------------------------------------
    def capture_reference(self):
        print("\n[REF] Capturing reference (board must be empty)...")
        time.sleep(0.3)
        for i, cap in enumerate(self.caps):
            for _ in range(15):  # Flush buffer
                cap.read()
            ret, frame = cap.read()
            if ret and i < len(self.calibrators):
                warped = self.calibrators[i].warp_frame(frame)
                self.detectors[i].set_reference(warped)
                print(f"  Cam {i}: OK")
        self._ref_captured_at = datetime.now().strftime("%H:%M")
        # Push immédiat : bool ref_ok est passé de False à True (ou se rafraîchit).
        self.bridge.push_status(self._build_system_snapshot())

    # -----------------------------------------------------------------
    # DETECTION + FUSION
    # -----------------------------------------------------------------
    def process_frame_cycle(self, warped_frames):
        """Run detection on all cameras, feed into fusion, return result."""
        for i, (warped, det) in enumerate(zip(warped_frames, self.detectors)):
            if warped is None:
                continue
            result = det.process_frame(warped)
            if result["state"] == "detected" and result["tip"] is not None:
                fused = self.fusion.add_detection(
                    cam_id=i,
                    tip=result["tip"],
                    diff_score=result["diff_score"],
                    ray=result.get("ray"),
                )
                if fused is not None:
                    return fused

        # Check if fusion timeout triggers a result
        fused = self.fusion.try_fuse()
        return fused

    # -----------------------------------------------------------------
    # DISPLAY
    # -----------------------------------------------------------------
    def draw_display(self, warped_frames):
        """Adaptive layout: cameras side by side + scoreboard."""
        visible = [(i, w) for i, w in enumerate(warped_frames)
                   if w is not None and i < len(self.cam_visible) and self.cam_visible[i]]

        n_vis = len(visible)
        panel_h = 650
        panel_w = 320 if n_vis >= 3 else 380
        score_w = 340
        total_w = panel_w * max(n_vis, 1) + score_w
        canvas = np.zeros((panel_h, total_w, 3), dtype=np.uint8)

        for slot, (cam_idx, warped) in enumerate(visible):
            display = warped.copy()

            if self.debug_mode:
                display = draw_board_overlay(
                    display, config.WARP_CENTER, config.WARP_CENTER, config.WARP_RADIUS
                )

            det = self.detectors[cam_idx] if cam_idx < len(self.detectors) else None
            if det:
                state_colors = {
                    "idle": config.COLOR_GREEN, "motion": config.COLOR_YELLOW,
                    "confirming": config.COLOR_ORANGE, "cooldown": config.COLOR_BLUE,
                }
                sc = state_colors.get(det.state, config.COLOR_WHITE)
                cv2.putText(display, f"C{cam_idx}:{det.state}", (10, 22),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, sc, 2)
                cv2.putText(display, f"D:{det.dart_count}", (10, 44),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, config.COLOR_WHITE, 1)

                # Confidence zone indicator
                if cam_idx < len(self.calibrators) and self.calibrators[cam_idx].cam_position_segment:
                    seg = self.calibrators[cam_idx].cam_position_segment
                    cv2.putText(display, f"Zone:{seg}", (10, 64),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.4, config.COLOR_CYAN, 1)

                # Debug: contour + candidate tip + ray
                if self.debug_mode and det.candidate_contour is not None:
                    cv2.drawContours(display, [det.candidate_contour], -1,
                                     config.COLOR_CYAN, 2)

                # Debug: draw axis ray
                if self.debug_mode and det.candidate_ray is not None:
                    r = det.candidate_ray
                    pt1 = (int(r[0][0]), int(r[0][1]))
                    pt2 = (int(r[1][0]), int(r[1][1]))
                    cv2.line(display, pt1, pt2, config.COLOR_MAGENTA, 1)

                if det.candidate_tip and det.state in ("confirming", "motion"):
                    cv2.drawMarker(display, det.candidate_tip, config.COLOR_YELLOW,
                                   cv2.MARKER_CROSS, 20, 2)

                # All confirmed tips
                for pt in det.all_detections:
                    cv2.circle(display, pt, 4, config.COLOR_GREEN, -1)
                    cv2.circle(display, pt, 6, config.COLOR_WHITE, 1)

                if det.last_detection:
                    cv2.circle(display, det.last_detection, 7, config.COLOR_GREEN, -1)
                    cv2.circle(display, det.last_detection, 9, config.COLOR_WHITE, 2)

                # Debug mask overlay
                if self.debug_mode and det.debug_mask is not None:
                    ms = cv2.resize(det.debug_mask, (120, 120))
                    mc = cv2.cvtColor(ms, cv2.COLOR_GRAY2BGR)
                    y1, x1 = 5, display.shape[1] - 125
                    if x1 > 0:
                        display[y1:y1+120, x1:x1+120] = mc

            resized = cv2.resize(display, (panel_w, panel_h))
            canvas[:, slot*panel_w:(slot+1)*panel_w] = resized

        # Scoreboard
        x_off = panel_w * max(n_vis, 1)
        self.render_scoreboard(canvas, x_off)

        return canvas

    # -----------------------------------------------------------------
    # RENDER UI OVERLAY
    # -----------------------------------------------------------------
    def render_scoreboard(self, canvas, base_x):
        w = config.WINDOW_WIDTH - base_x
        h = config.WINDOW_HEIGHT
        x_off = base_x
        
        # Base transparent overlay for the whole scoreboard area
        overlay = canvas.copy()
        cv2.rectangle(overlay, (x_off, 0), (config.WINDOW_WIDTH, h), (15, 15, 20), -1)
        
        # Top banner background
        cv2.rectangle(overlay, (x_off, 0), (config.WINDOW_WIDTH, 50), (30, 30, 40), -1)
        cv2.addWeighted(overlay, 0.85, canvas, 0.15, 0, canvas)

        # Title
        mode_text = self.game.mode.upper() if self.game.mode != "free" else "FREE PLAY"
        cv2.putText(canvas, f"DARTVISION v3 - {mode_text}", (x_off+15, 32),
                    cv2.FONT_HERSHEY_DUPLEX, 0.65, config.COLOR_CYAN, 1)

        # Game Over Overlay
        if self.game.game_over:
            dim_overlay = canvas.copy()
            cv2.rectangle(dim_overlay, (x_off, 50), (config.WINDOW_WIDTH, h), (0, 0, 0), -1)
            cv2.addWeighted(dim_overlay, 0.7, canvas, 0.3, 0, canvas)
            
            # Winner text
            winner_text = f"{self.game.winner} WINS!"
            text_sz, _ = cv2.getTextSize(winner_text, cv2.FONT_HERSHEY_DUPLEX, 1.2, 2)
            tx = x_off + (w - text_sz[0]) // 2
            ty = h // 3
            
            # Subtle glow effect
            cv2.putText(canvas, winner_text, (tx+2, ty+2), cv2.FONT_HERSHEY_DUPLEX, 1.2, (0,0,0), 4)
            cv2.putText(canvas, winner_text, (tx, ty), cv2.FONT_HERSHEY_DUPLEX, 1.2, config.COLOR_GREEN, 2)
            
            sub_text = "[ NEXT TURN ] to start / 'r' to restart"
            sub_sz, _ = cv2.getTextSize(sub_text, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 1)
            cv2.putText(canvas, sub_text, (x_off + (w - sub_sz[0]) // 2, ty + 50),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, config.COLOR_WHITE, 1)
            return

        y = 70
        board = self.game.get_scoreboard()
        for pdata in board:
            # Active player highlight box
            if pdata["active"]:
                box_overlay = canvas.copy()
                cv2.rectangle(box_overlay, (x_off+5, y-20), (config.WINDOW_WIDTH-5, y+65 + (20 if pdata["checkout"] else 0)), (40, 80, 40), -1)
                cv2.addWeighted(box_overlay, 0.5, canvas, 0.5, 0, canvas)
                color_name = config.COLOR_GREEN
                cv2.putText(canvas, ">>>", (x_off+10, y), cv2.FONT_HERSHEY_DUPLEX, 0.65, config.COLOR_GREEN, 2)
                name_x = x_off + 55
            else:
                color_name = (200, 200, 200)
                name_x = x_off + 25

            cv2.putText(canvas, pdata["name"], (name_x, y),
                        cv2.FONT_HERSHEY_DUPLEX, 0.65, color_name, 1)

            # Score
            score_txt = str(pdata["score"])
            score_sz, _ = cv2.getTextSize(score_txt, cv2.FONT_HERSHEY_DUPLEX, 1.4, 2)
            cv2.putText(canvas, score_txt, (x_off + w - score_sz[0] - 20, y+5),
                        cv2.FONT_HERSHEY_DUPLEX, 1.4, color_name, 2)

            y += 25
            darts = " ".join(["[X]" if j < pdata['darts'] else "[ ]" for j in range(3)])
            cv2.putText(canvas, f"Darts: {darts}",
                        (name_x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.45, config.COLOR_WHITE, 1)
            
            y += 20
            cv2.putText(canvas, f"Avg: {pdata['avg']:.1f}   Turns: {pdata['turns']}",
                        (name_x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (180, 180, 180), 1)
            
            y += 20
            if pdata["checkout"]:
                cv2.putText(canvas, f"Out: {' / '.join(pdata['checkout'])}",
                            (name_x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.45, config.COLOR_YELLOW, 1)
                y += 20
                
            y += 15

        # Last score panel at the bottom
        if self.last_score:
            last_y = h - 140
            
            # Panel background
            lp_overlay = canvas.copy()
            cv2.rectangle(lp_overlay, (x_off+10, last_y), (config.WINDOW_WIDTH-10, h-60), (40, 40, 50), -1)
            cv2.addWeighted(lp_overlay, 0.7, canvas, 0.3, 0, canvas)
            
            cv2.putText(canvas, "LAST DART", (x_off+20, last_y+20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (150, 150, 150), 1)
            
            # Big label
            label = self.last_score["label"]
            label_col = config.COLOR_RED if label == "MISS" else config.COLOR_GREEN
            cv2.putText(canvas, label, (x_off+20, last_y+55),
                        cv2.FONT_HERSHEY_DUPLEX, 1.2, label_col, 2)
            
            pts = self.last_score['score']
            cv2.putText(canvas, f"{pts} pts", (x_off+20, last_y+80),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, config.COLOR_WHITE, 1)
                        
            # Technical details
            method = self.last_score.get('fusion_method', '?')
            cams = self.last_score.get('fusion_cams', [])
            conf = self.last_score.get('fusion_confidence', 0)
            cv2.putText(canvas, f"Sys: {method} | Cams: {cams} | Conf: {conf:.0%}",
                        (x_off+140, last_y+80), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (130, 130, 130), 1)

        # Buttons definitions
        self.buttons = []
        btn_y = h - 90
        btn_h = 35
        
        button_defs = [
            ("UNDO DART", ord('u'), (80, 80, 100)),
            ("NEXT TURN", ord('n'), (100, 80, 80)),
            ("RESET", ord('r'), (60, 60, 60)),
            ("MODE", ord('m'), (60, 80, 100))
        ]
        
        btn_x = x_off + 10
        for label, key, bg_col in button_defs:
            sz, _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
            btn_w = sz[0] + 30
            self.buttons.append((label, btn_x, btn_y, btn_w, btn_h, key))
            
            # Draw Button
            cv2.rectangle(canvas, (btn_x, btn_y), (btn_x + btn_w, btn_y + btn_h), bg_col, -1)
            cv2.rectangle(canvas, (btn_x, btn_y), (btn_x + btn_w, btn_y + btn_h), (200, 200, 200), 1)
            cv2.putText(canvas, label, (btn_x + 15, btn_y + 22),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, config.COLOR_WHITE, 1)
            
            btn_x += btn_w + 10

        # Controls footer
        y = h - 25
        cv2.putText(canvas, "Hotkeys: SPC:ref  D:debug  1-3:cam  Q:quit",
                    (x_off+10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (120, 120, 120), 1)

    # -----------------------------------------------------------------
    # EVENT HANDLING
    # -----------------------------------------------------------------
    def _handle_detection(self, score_data):
        self.last_score = score_data
        self.last_score_time = time.time()

        result = self.game.register_throw(score_data)
        t = result["throw"]
        player = result["player"]

        method = score_data.get('fusion_method', '?')
        cams = score_data.get('fusion_cams', [])
        conf = score_data.get('fusion_confidence', 0)

        print(f"  >> {player}: {t.label} ({t.score} pts)"
              f"  [Cams {cams}, {method}, {conf:.0%}]", end="")

        if result["bust"]:
            print("  BUST!", end="")
        if result["game_over"]:
            print(f"\n  === {result['winner']} WINS! ===")
        if result["turn_complete"]:
            print("  [End turn]", end="")
        print()

    def _mouse_callback(self, event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            for (label, bx, by, bw, bh, key) in self.buttons:
                if bx <= x <= bx + bw and by <= y <= by + bh:
                    self._handle_key(key)
                    break

    def _handle_key(self, key):
        if key in (ord('q'), 27):
            self.running = False
        elif key == ord(' '):
            print("\n[REF] Recapturing reference...")
            self.capture_reference()
        elif key == ord('u'):
            print("  Undo" if self.game.undo_last_throw() else "  Nothing to undo")
        elif key == ord('n'):
            self.game.force_next_turn()
            print("  Next turn")
        elif key == ord('d'):
            self.debug_mode = not self.debug_mode
            print(f"  Debug: {'ON' if self.debug_mode else 'OFF'}")
        elif key == ord('m'):
            # Cycle through modes
            modes = GameEngine.MODES
            next_idx = (modes.index(self.game.mode) + 1) % len(modes)
            new_mode = modes[next_idx]
            player_names = [p.name for p in self.game.players]
            self.game = GameEngine(mode=new_mode, player_names=player_names)
            print(f"\n[MODE] Switched to {new_mode.upper()}")
            # Reset detection on mode change
            for det in self.detectors:
                det.reset()
            self.capture_reference()
            self.last_score = None
        elif key == ord('r'):
            print("\n[RESET] Full reset & Restart Game...")
            player_names = [p.name for p in self.game.players]
            self.game = GameEngine(mode=self.game.mode, player_names=player_names)
            for det in self.detectors:
                det.reset()
            time.sleep(0.3)
            self.capture_reference()
            self.last_score = None
        elif key == ord('c'):
            self.recalibrate = True
            self.calibrate()
            self.capture_reference()
        elif ord('1') <= key <= ord('9'):
            idx = key - ord('1')
            if idx < len(self.cam_visible):
                self.cam_visible[idx] = not self.cam_visible[idx]
                print(f"  Cam {idx}: {'visible' if self.cam_visible[idx] else 'hidden'}")

    # -----------------------------------------------------------------
    # MAIN LOOP
    # -----------------------------------------------------------------
    def run(self):
        if not self.init_cameras():
            return
        if not self.calibrate():
            print("Calibration failed.")
            return

        self.capture_reference()

        # En mode headless (--headless) on skip toute interaction OpenCV.
        # La fenêtre de debug devient inutile dès qu'on a l'UI web ; ce flag
        # permet aussi de tester via SSH sans X server.
        if not self.headless:
            cv2.namedWindow("DartVision", cv2.WINDOW_NORMAL)
            cv2.resizeWindow("DartVision", config.WINDOW_WIDTH, config.WINDOW_HEIGHT)
            cv2.setMouseCallback("DartVision", self._mouse_callback)

        print(f"\n{'='*60}")
        print(f"DARTVISION v3 - {len(self.caps)} CAMERAS")
        print(f"{'='*60}")
        print("SPACE = set reference | d = debug | q = quit\n")

        while self.running:
            try:
                # Recalibration demandée par le WebSocket (touche 'c' clavier
                # passe directement par _handle_key).
                if self._recalib_requested:
                    self._recalib_requested = False
                    self.recalibrate = True
                    self.calibrate()
                    self.capture_reference()

                warped_frames = []
                if len(self._last_cam_read) != len(self.caps):
                    self._last_cam_read = [0.0] * len(self.caps)
                now_ts = time.time()
                for i, cap in enumerate(self.caps):
                    ret, frame = cap.read()
                    if ret:
                        self._last_cam_read[i] = now_ts
                    if ret and i < len(self.calibrators):
                        warped_frames.append(self.calibrators[i].warp_frame(frame))
                    else:
                        warped_frames.append(None)

                # Detection + fusion
                fused = self.process_frame_cycle(warped_frames)
                if fused is not None:
                    self._handle_detection(fused)

                # Render OpenCV uniquement si on n'est pas headless
                if not self.headless:
                    display = self.draw_display(warped_frames)
                    cv2.imshow("DartVision", display)

                # Push frames JPEG vers le bridge (un par slot mappé).
                # ~20 fps suffisent pour le streaming web ; on évite de re-encoder
                # à chaque frame si la cam pousse plus rapidement (config.CAM_FPS).
                self._push_frames_to_bridge(warped_frames)

                # Push status système ~2 Hz (le bridge re-throttle au besoin).
                now = time.time()
                if now - self._last_status_emit >= 0.5:
                    self._last_status_emit = now
                    self.bridge.push_status(self._build_system_snapshot())

                if self.headless:
                    time.sleep(0.03)  # ~30ms tick comme cv2.waitKey(30)
                else:
                    key = cv2.waitKey(30) & 0xFF
                    self._handle_key(key)

            except Exception as e:
                print(f"\n[ERROR] {type(e).__name__}: {e}")
                import traceback
                traceback.print_exc()
                print("Continuing...\n")

        for cap in self.caps:
            cap.release()
        cv2.destroyAllWindows()
        print("\nDartVision closed.")

    def _push_frames_to_bridge(self, warped_frames):
        """Encode chaque frame warpée en JPEG et la dépose dans le bridge.

        Mappage cam_index → slot via config.CAM_SLOTS. Si l'index physique
        n'a pas de slot, on skip silencieusement.
        """
        slot_by_index = {
            spec["index"]: spec["slot"]
            for spec in getattr(config, "CAM_SLOTS", [])
        }
        if not slot_by_index:
            return
        for i, warped in enumerate(warped_frames):
            if warped is None or i >= len(self.calibrators):
                continue
            phys_idx = self.calibrators[i].cam_index
            slot = slot_by_index.get(phys_idx)
            if slot is None:
                continue
            try:
                ok, jpg = cv2.imencode(".jpg", warped,
                                        [int(cv2.IMWRITE_JPEG_QUALITY), 75])
                if ok:
                    self.bridge.set_frame(slot, jpg.tobytes())
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description="DartVision v3")
    parser.add_argument("--mode", choices=["free", "501", "301"],
                        default="free")
    parser.add_argument("--players", type=int, default=1)
    parser.add_argument("--names", nargs="+", default=None)
    parser.add_argument("--cams", nargs="+", type=int, default=None,
                        help="Camera indexes (e.g. --cams 8 6 4)")
    parser.add_argument("--cam-positions", nargs="+", type=int, default=None,
                        help="Segment each cam faces (e.g. --cam-positions 11 18 6)")
    parser.add_argument("--recalibrate", action="store_true")
    parser.add_argument("--headless", action="store_true",
                        help="Pas de fenêtre OpenCV (UI accessible via le web uniquement). "
                             "Utile pour les sessions SSH ou le déploiement kiosque.")

    args = parser.parse_args()

    if args.cams:
        config.CAM_INDEXES = args.cams
    if args.cam_positions:
        config.CAM_POSITIONS = args.cam_positions

    app = DartVision(
        mode=args.mode,
        num_players=args.players,
        player_names=args.names,
        recalibrate=args.recalibrate,
        cam_positions=args.cam_positions,
        headless=args.headless,
    )
    app.run()


if __name__ == "__main__":
    main()
