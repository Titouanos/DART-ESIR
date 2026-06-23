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
import os
import time
from datetime import datetime
from typing import Optional, Union

import logging
import cv2
import numpy as np


def _resolve_cam_source(spec: dict) -> Union[str, int]:
    """Retourne ce qu'on passera à cv2.VideoCapture pour ce slot cam.

    - Si `spec['device']` est défini ET le path existe (symlink udev résolu),
      on l'utilise — c'est la voie production stable face aux replug USB.
    - Sinon on retombe sur `spec['index']` (mode dev sans udev, ou path
      manquant temporairement).
    """
    dev = spec.get("device")
    if dev and os.path.exists(dev):
        return dev
    return int(spec["index"])


class CamHealthCheckError(SystemExit):
    """Levée si un slot cam n'est pas dans un état exploitable au boot."""

    def __init__(self, msg: str):
        super().__init__(f"\n[FATAL] {msg}\n")


def _health_check_cams() -> None:
    """Vérifie que chaque slot CAM_SLOTS est utilisable AVANT de tout démarrer.

    Pour chaque slot avec un `device` path :
      1. path existe sur disque (sinon symlink udev cassé)
      2. cv2.VideoCapture(path).isOpened() (sinon device inaccessible)
      3. cap.read() retourne une frame (sinon c'est probablement un Metadata
         Capture node qu'on a chopé par accident — symlink udev mal targuetté)

    Dernière ligne de défense avant qu'un kiosque ne démarre avec un mapping
    silencieusement cassé (= scores aberrants sans message d'erreur). Mieux
    vaut planter clean au boot.
    """
    for spec in config.CAM_SLOTS:
        slot = spec.get("slot", "?")
        dev = spec.get("device")
        if not dev:
            # Mode int-index (--cams CLI) — le init_cameras() classique fera
            # le check de disponibilité, on ne fait rien ici.
            continue

        # 1) existence du path
        if not os.path.exists(dev):
            raise CamHealthCheckError(
                f"Slot {slot} : device path {dev} introuvable.\n"
                f"Vérifie les symlinks udev :\n"
                f"  ls -l /dev/dart-cam-*\n"
                f"Si absents, ré-installe les règles :\n"
                f"  sudo cp webui/udev/99-dart-cams.rules /etc/udev/rules.d/\n"
                f"  sudo udevadm control --reload\n"
                f"  sudo udevadm trigger --subsystem-match=video4linux --action=add"
            )

        # 2) open
        cap = cv2.VideoCapture(dev)
        if not cap.isOpened():
            try: cap.release()
            except Exception: pass
            raise CamHealthCheckError(
                f"Slot {slot} : cv2.VideoCapture({dev}) refuse l'open.\n"
                f"Device peut être tenu par un autre process. Check :\n"
                f"  sudo fuser {dev}\n"
                f"  pgrep -af main.py"
            )

        # 3) read — distingue Video Capture (renvoie frame) de Metadata
        #    Capture (open OK, read False).
        ok, _ = cap.read()
        cap.release()
        if not ok:
            raise CamHealthCheckError(
                f"Slot {slot} : {dev} s'ouvre mais ne renvoie pas de frame.\n"
                f"C'est probablement un Metadata Capture node (Device Caps "
                f"0x04a00000). Le symlink udev pointe au mauvais endroit.\n"
                f"Régénère les symlinks :\n"
                f"  sudo udevadm control --reload\n"
                f"  sudo udevadm trigger --subsystem-match=video4linux --action=change\n"
                f"  ls -l /dev/dart-cam-*\n"
                f"  v4l2-ctl -d {dev} --info | grep 'Device Caps' -A2"
            )

    print(f"[HEALTH] Cam health check OK pour les {len(config.CAM_SLOTS)} slot(s)")

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
        # Takeout : non-None quand on attend le retrait des fléchettes
        # (fin de tour). Voir start_takeout() / _process_takeout().
        self._takeout: Optional[dict] = None
        # Compteur de frames noires consécutives par cam (cam branchée mais
        # image inutilisable — exposition/USB). Voir boucle run().
        self._cam_black: list = []
        # Confirmations post-lancer différées : {cam_idx: frames restantes}.
        # Voir _post_throw_sync / _drain_pending_confirms.
        self._pending_confirm: dict = {}

    # -----------------------------------------------------------------
    # INIT
    # -----------------------------------------------------------------
    def init_cameras(self) -> bool:
        """Test que les cams existent (ouverture/fermeture une à la fois)."""
        print("\n[INIT] Testing cameras...")
        valid_indexes = []
        valid_slots = []
        for spec in config.CAM_SLOTS:
            src = _resolve_cam_source(spec)
            tag = f"slot {spec['slot']} via {src}"
            cap = cv2.VideoCapture(src)
            if cap.isOpened():
                # `read()` confirme que ce n'est pas juste un open ouvert mais
                # bien un capture qui délivre des frames (V4L2 metadata nodes
                # passent isOpened mais ne renvoient rien).
                ok, _ = cap.read()
                if ok:
                    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    print(f"  Camera {tag}: OK ({w}x{h})")
                    valid_indexes.append(spec["index"])
                    valid_slots.append(spec)
                else:
                    print(f"  Camera {tag}: OPEN mais pas de frame (metadata node?)")
                cap.release()
            else:
                print(f"  Camera {tag}: NOT FOUND")

        if not valid_indexes:
            print("ERROR: No cameras found.")
            return False

        config.CAM_INDEXES = valid_indexes
        config.CAM_SLOTS = valid_slots
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
        """Ouvre toutes les cams pour la boucle principale (path udev si dispo).

        On itère sur CAM_SLOTS (donne accès au device path + index + master),
        et on garde `config.CAM_INDEXES` synchro pour la compat backend
        (calibration.json est encore keyé par index entier).
        """
        self.caps = []
        self.detectors = []
        print("\n[INIT] Opening all cameras...")
        for i, spec in enumerate(config.CAM_SLOTS):
            src = _resolve_cam_source(spec)
            tag = f"slot {spec['slot']} via {src}"
            cap = cv2.VideoCapture(src)
            if not cap.isOpened():
                print(f"  WARN: Camera {tag} failed to open")
                continue
            # MJPEG uses way less USB bandwidth than raw YUYV
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('M','J','P','G'))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, config.CAM_WIDTH)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, config.CAM_HEIGHT)
            cap.set(cv2.CAP_PROP_FPS, config.CAM_FPS)
            # Buffer V4L2 minimal : sans ça chaque cam accumule ~4 frames et
            # read() rend des images PÉRIMÉES, avec un retard différent par
            # cam → les caméras ne voient pas la même scène au même instant
            # (désynchro observée : CAM B affichait 2 darts quand A en avait 3)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            self.caps.append(cap)
            self.detectors.append(DartDetector(cam_id=i))
            print(f"  Camera {tag}: {w}x{h} MJPEG")
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

        Note : en mode headless, utiliser le flow de recalibration WEB via
        /api/calibration/compute + /api/calibration/save (cf. webui/server.py).
        """
        if self.headless:
            raise RuntimeError(
                "Recalibration interactive OpenCV non disponible en mode "
                "--headless. Utilise le bouton 'Recalibrer' du web qui passe "
                "par /api/calibration/compute + save (sans Qt)."
            )
        self._recalib_requested = True

    def request_shutdown(self) -> None:
        self.running = False

    def reload_calibration(self) -> bool:
        """Recharge calibration.json + relance le fusion engine.

        Appelé par le bridge après que /api/calibration/save ait écrit un
        nouveau set de points. Pas besoin de restart main.py : on remplace
        les Calibrator existants et on régénère le FusionEngine.
        """
        loaded = load_calibrations()
        by_idx = {c.cam_index: c for c in loaded}
        ordered = [by_idx[i] for i in config.CAM_INDEXES if i in by_idx]
        if not ordered or len(ordered) != len(config.CAM_INDEXES):
            print(f"[CALIB] reload échoué : "
                  f"trouvé {len(ordered)}/{len(config.CAM_INDEXES)} calibrators")
            return False
        self.calibrators = ordered
        self._init_fusion()
        # Reset les détecteurs (la référence devient invalide après changement
        # d'homographie). L'utilisateur devra recapturer la référence.
        # Le bearing appris est aussi invalidé : il est exprimé en coordonnées
        # warpées, qui viennent de changer — il se réapprendra en 2-3 lancers.
        for det in self.detectors:
            det.reset()
            det.cam_bearing = None
        self._ref_captured_at = None
        print(f"[CALIB] reload OK ({len(ordered)} calibrators) — "
              f"recapture la référence !")
        return True

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
                    # Cam qui lit mais renvoie du noir = inutilisable
                    if pos < len(self._cam_black) and self._cam_black[pos] >= 30:
                        cap_ok = False
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
            # Tuning live des seuils — hydrate les sliders au load + reflète
            # la valeur courante quand `set_tuning` est envoyé par un client.
            "tuning": {
                "diff_threshold": int(config.DIFF_THRESHOLD),
                "min_dart_area": int(config.MIN_DART_AREA),
                "stable_frames": int(config.STABLE_FRAMES),
                "min_elongation": float(config.MIN_ELONGATION),
            },
        }

    # -----------------------------------------------------------------
    # REFERENCE CAPTURE
    # -----------------------------------------------------------------
    def capture_reference(self):
        print("\n[REF] Capturing reference (board must be empty)...")
        self._takeout = None   # toute capture manuelle sort du mode takeout
        self._pending_confirm.clear()
        time.sleep(0.3)
        for i, cap in enumerate(self.caps):
            # Flush minimal : BUFFERSIZE=1 côté V4L2, 4 lectures suffisent.
            # (15 lectures bloquaient la boucle ~3-4s → flux MJPEG figés,
            # perçu comme un "crash des cams" après chaque fin de tour.)
            for _ in range(4):
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
    def is_in_takeout(self) -> bool:
        """True si un tour vient de finir et qu'on attend le retrait des
        fléchettes (détection en pause). Lu par le bridge pour ne pas
        double-avancer si l'utilisateur clique 'joueur suivant' à ce moment."""
        return self._takeout is not None

    def cancel_takeout(self):
        """Annule un takeout en cours et remet la détection live tout de suite.
        Appelé sur reset/quit : le joueur veut rejouer immédiatement, pas
        attendre la fin du cycle de retrait."""
        if self._takeout is not None:
            print("[TAKEOUT] annulé (reset/quit) — détection live")
        self._takeout = None
        self._pending_confirm.clear()
        for det in self.detectors:
            if det.state == "takeout":
                det.state = "idle"
                det.stable_count = 0

    def start_takeout(self):
        """Fin de tour : suspend la détection jusqu'au retrait des fléchettes.

        Sans cette pause, le diff vs référence (qui contient encore les darts)
        voit les silhouettes des fléchettes retirées + les trous laissés par
        les pointes, et les prend pour de nouveaux impacts. La référence est
        recapturée une fois le retrait observé (cf. _process_takeout), ce qui
        absorbe aussi les trous dans la nouvelle référence.
        """
        if self._takeout is not None:
            return
        self._takeout = {"since": time.time(), "activity": False, "stable": 0}
        self._pending_confirm.clear()
        for det in self.detectors:
            det.enter_takeout()
        self.fusion.pending.clear()
        print("\n[TAKEOUT] Turn over - waiting for darts to be removed...")

    def _process_takeout(self, warped_frames):
        """Observe motion/diff pendant le retrait. Sortie quand :
        activité vue (main dans le champ) PUIS stabilité prolongée, avec
        confirmation par le diff (silhouettes des darts retirées visibles)
        — ou timeout de sécurité. Recapture alors la référence."""
        tk = self._takeout
        max_motion = 0
        max_diff = 0.0
        for warped, det in zip(warped_frames, self.detectors):
            if warped is None:
                continue
            r = det.process_frame(warped)
            max_motion = max(max_motion, r.get("motion", 0))
            max_diff = max(max_diff, r.get("diff_score", 0.0))

        if max_motion > config.TAKEOUT_ACTIVITY_MOTION:
            tk["activity"] = True
            tk["stable"] = 0
            return
        if max_motion < 800:
            tk["stable"] += 1
        else:
            tk["stable"] = 0

        elapsed = time.time() - tk["since"]
        stable_enough = tk["stable"] >= config.TAKEOUT_STABLE_FRAMES
        removal_seen = tk["activity"] and max_diff > config.TAKEOUT_MIN_DIFF_AREA

        # Sortie rapide : retrait constaté + board calme (cas nominal).
        # Filet souple : calme prolongé seul, passé TAKEOUT_TIMEOUT_S.
        # Plafond DUR : on débloque le joueur suivant quoi qu'il arrive — sinon
        # un board qui ne se calme jamais (bruit cam) bloquait J2 30-70s.
        reason = None
        if removal_seen and stable_enough:
            reason = "retrait constaté + calme"
        elif stable_enough and elapsed > config.TAKEOUT_TIMEOUT_S:
            reason = f"calme prolongé ({elapsed:.0f}s)"
        elif elapsed > config.TAKEOUT_MAX_S:
            reason = f"plafond {config.TAKEOUT_MAX_S:.0f}s (forcé)"

        if reason:
            print(f"\n[TAKEOUT] {reason} — recapture référence, au joueur suivant")
            for det in self.detectors:
                det.start_new_turn()
            self.capture_reference()   # remet aussi _takeout à None
            # Grâce post-takeout : si le joueur traîne encore près du board,
            # son départ ne doit pas être pris pour un lancer.
            for det in self.detectors:
                det.cooldown = max(det.cooldown, 20)

    def _post_throw_sync(self, warped_frames):
        """Après un lancer scoré : toutes les cams absorbent la fléchette.

        confirm_detection() remet la référence de CHAQUE cam à la frame
        courante (fléchette incluse) + petit cooldown. Les cams retardataires
        ne verront donc plus la fléchette comme un nouveau diff.

        Subtilité : si une cam voit du MOUVEMENT à cet instant (le bras du
        lanceur encore dans SON champ), confirmer maintenant figerait le bras
        dans sa référence — et son départ "révélerait" la fléchette qu'il
        occultait → re-score (vu en prod : S15 scoré 2× à 4s d'écart). On
        diffère la confirmation jusqu'au calme sur cette cam.
        """
        for i, (det, warped) in enumerate(zip(self.detectors, warped_frames)):
            if warped is None or det.state == "takeout":
                continue
            det.state = "idle"
            det.stable_count = 0
            det.cooldown = max(det.cooldown, 10)
            if det.last_motion > 1200:
                self._pending_confirm[i] = 150   # ~5s max, confirme au calme
            else:
                det.confirm_detection(warped)
                self._pending_confirm.pop(i, None)
        self.fusion.pending.clear()

    def _drain_pending_confirms(self, warped_frames):
        """Confirme les cams dont la synchro post-lancer a été différée
        (bras dans le champ au moment du score), dès que le calme revient."""
        for i in list(self._pending_confirm):
            det = self.detectors[i] if i < len(self.detectors) else None
            warped = warped_frames[i] if i < len(warped_frames) else None
            if det is None or warped is None or det.state == "takeout":
                self._pending_confirm.pop(i, None)
                continue
            self._pending_confirm[i] -= 1
            if det.last_motion < 800 or self._pending_confirm[i] <= 0:
                det.confirm_detection(warped)
                det.state = "idle"
                det.stable_count = 0
                det.cooldown = max(det.cooldown, 5)
                self._pending_confirm.pop(i, None)

    def process_frame_cycle(self, warped_frames):
        """Run detection on all cameras, feed into fusion, return result."""
        if self._takeout is not None:
            self._process_takeout(warped_frames)
            return None
        if self._pending_confirm:
            self._drain_pending_confirms(warped_frames)
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

        # Diagnostic systématique : tips 2D par cam + résidu géométrique +
        # état des cams. Indispensable pour auditer chaque score a posteriori.
        tips = score_data.get('fusion_tips', {})
        residual = score_data.get('fusion_residual_px')
        cam_states = [d.state for d in self.detectors]
        print(f"  [DIAG] fused={score_data.get('tip_px')} tips/cam={tips} "
              f"residu={residual}px states={cam_states} "
              f"black={[b >= 30 for b in self._cam_black]}")

        print(f"  >> {player}: {t.label} ({t.score} pts)"
              f"  [Cams {cams}, {method}, {conf:.0%}]", end="")

        if result["bust"]:
            print("  BUST!", end="")
        if result["game_over"]:
            print(f"\n  === {result['winner']} WINS! ===")
        if result["turn_complete"]:
            print("  [End turn]", end="")
        print()

        # Fin de tour (3 darts, bust ou win) → le joueur va retirer ses
        # fléchettes : on suspend la détection pour ne pas scorer les
        # silhouettes/trous du retrait.
        if result["turn_complete"]:
            self.start_takeout()

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
        # Health check tôt : si les symlinks udev sont cassés ou pointent vers
        # un Metadata Capture, on échoue clean avec un message actionable
        # plutôt que de démarrer un kiosque silencieusement bancal.
        _health_check_cams()
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
                if len(self._cam_black) != len(self.caps):
                    self._cam_black = [0] * len(self.caps)
                now_ts = time.time()
                # Capture en 2 temps : grab() (dequeue rapide) sur TOUTES les
                # cams d'abord, puis retrieve() (décodage). Les 3 captures
                # tombent ainsi à quelques ms d'écart au lieu d'être décalées
                # par le temps de décodage/warp de chaque cam précédente.
                grabbed = [cap.grab() for cap in self.caps]
                for i, cap in enumerate(self.caps):
                    ret, frame = cap.retrieve() if grabbed[i] else (False, None)
                    if ret:
                        self._last_cam_read[i] = now_ts
                        # Cam "vivante" mais image noire (exposition/USB HS) :
                        # aussi inutilisable qu'une cam morte → on le signale.
                        if frame[::16, ::16].mean() < 5.0:
                            self._cam_black[i] += 1
                            if self._cam_black[i] == 30:
                                print(f"[CAM] ATTENTION: cam {i} renvoie des "
                                      f"frames NOIRES (objectif/expo/USB ?)")
                        else:
                            if self._cam_black[i] >= 30:
                                print(f"[CAM] cam {i} : signal revenu")
                            self._cam_black[i] = 0
                        # Push frame RAW (pré-warp) au bridge pour la recalibration web.
                        # Qualité ~70 (suffisant pour clic 4-points, économise CPU).
                        if i < len(config.CAM_SLOTS):
                            slot = config.CAM_SLOTS[i].get("slot")
                            if slot:
                                try:
                                    ok_r, jpg_r = cv2.imencode(".jpg", frame,
                                                                [int(cv2.IMWRITE_JPEG_QUALITY), 70])
                                    if ok_r:
                                        self.bridge.set_frame_raw(slot, jpg_r.tobytes())
                                except Exception:
                                    pass
                    if ret and i < len(self.calibrators):
                        warped_frames.append(self.calibrators[i].warp_frame(frame))
                    else:
                        warped_frames.append(None)

                # Detection + fusion
                fused = self.process_frame_cycle(warped_frames)
                if fused is not None:
                    self._handle_detection(fused)
                    # CRUCIAL : resynchronise TOUTES les cams sur ce lancer.
                    # Sans ça, les cams qui n'ont pas participé à la fusion
                    # gardent une référence pré-impact et re-scorent la même
                    # fléchette quelques secondes plus tard en "single".
                    self._post_throw_sync(warped_frames)

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

        On pousse 2 versions par cam :
          - frame propre (`set_frame`)             → /api/cam/X/mjpeg
          - frame annotée debug (`set_frame_debug`) → /api/cam/X/debug.mjpeg
            (overlay : état detector, contour, ray, candidate tip, derniers
             impacts confirmés, mini masque diff en coin)

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
                # 1) Frame propre — pour l'UI normale
                ok, jpg = cv2.imencode(".jpg", warped,
                                        [int(cv2.IMWRITE_JPEG_QUALITY), 75])
                if ok:
                    self.bridge.set_frame(slot, jpg.tobytes())

                # 2) Frame annotée — pour le bouton debug. Downscalée à 560px :
                # affichée en tiers d'écran dans l'overlay, et le hotspot ne
                # tient pas 3 flux 800px (streams qui meurent par à-coups).
                det = self.detectors[i] if i < len(self.detectors) else None
                if det is not None:
                    annotated = self._annotate_debug(warped, det, slot)
                    annotated = cv2.resize(annotated, (560, 560),
                                           interpolation=cv2.INTER_AREA)
                    ok_d, jpg_d = cv2.imencode(".jpg", annotated,
                                                [int(cv2.IMWRITE_JPEG_QUALITY), 65])
                    if ok_d:
                        self.bridge.set_frame_debug(slot, jpg_d.tobytes())
            except Exception:
                pass

    def _annotate_debug(self, warped, detector, slot):
        """Dessine sur une copie de `warped` les overlays de debug détection."""
        out = warped.copy()
        # En-tête : slot + état du détecteur + nb darts captés
        state_colors = {
            "idle":       config.COLOR_GREEN,
            "motion":     config.COLOR_YELLOW,
            "confirming": config.COLOR_ORANGE,
            "cooldown":   config.COLOR_BLUE,
            "detected":   config.COLOR_GREEN,
            "takeout":    config.COLOR_MAGENTA,
        }
        sc = state_colors.get(detector.state, config.COLOR_WHITE)
        cv2.putText(out, f"CAM {slot} | {detector.state.upper()}", (10, 26),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.62, sc, 2)
        cv2.putText(out, f"darts: {detector.dart_count}", (10, 50),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, config.COLOR_WHITE, 1)

        # Pourquoi un blob diff n'a pas donné de candidat : compteurs de
        # rejets de la dernière frame (trop petit / trop grand / ombre)
        rej = getattr(detector, "last_rejections", None)
        if rej and any(rej.values()):
            cv2.putText(out, f"rej p:{rej['small']} g:{rej['big']} o:{rej['shadow']}",
                        (10, 72), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                        config.COLOR_ORANGE, 1)

        # Label + score de la dernière détection (résultat APRÈS fusion).
        # Affiché en gros en bas-gauche pour lire de loin.
        if self.last_score:
            label = self.last_score.get("label", "?")
            pts   = self.last_score.get("score", 0)
            method = self.last_score.get("fusion_method", "")
            conf  = self.last_score.get("fusion_confidence", 0) or 0
            label_col = config.COLOR_GREEN if pts > 0 else config.COLOR_RED
            y0 = out.shape[0] - 50
            # Ombre noire pour lisibilité sur fond clair/sombre variable
            cv2.putText(out, f"{label} = {pts}", (12, y0+2),
                        cv2.FONT_HERSHEY_DUPLEX, 1.1, (0, 0, 0), 4, cv2.LINE_AA)
            cv2.putText(out, f"{label} = {pts}", (10, y0),
                        cv2.FONT_HERSHEY_DUPLEX, 1.1, label_col, 2, cv2.LINE_AA)
            cv2.putText(out, f"{method} {conf*100:.0f}%",
                        (10, y0 + 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.42,
                        config.COLOR_WHITE, 1, cv2.LINE_AA)

        # Cible (cercles concentriques) pour valider la calibration visuellement
        cx, cy, r = config.WARP_CENTER, config.WARP_CENTER, config.WARP_RADIUS
        for rr in (r, int(r*0.95), int(r*0.63), int(r*0.59), int(r*0.094), int(r*0.038)):
            cv2.circle(out, (cx, cy), rr, config.COLOR_CYAN, 1, cv2.LINE_AA)
        cv2.drawMarker(out, (cx, cy), config.COLOR_CYAN, cv2.MARKER_CROSS, 14, 1)

        # Contour candidat (cyan)
        if detector.candidate_contour is not None:
            try:
                cv2.drawContours(out, [detector.candidate_contour], -1,
                                 config.COLOR_CYAN, 2)
            except Exception:
                pass

        # Ray = axe principal de la fléchette détectée (magenta)
        if detector.candidate_ray is not None:
            try:
                r0 = (int(detector.candidate_ray[0][0]), int(detector.candidate_ray[0][1]))
                r1 = (int(detector.candidate_ray[1][0]), int(detector.candidate_ray[1][1]))
                cv2.line(out, r0, r1, config.COLOR_MAGENTA, 1, cv2.LINE_AA)
            except Exception:
                pass

        # Tip candidat en cours (jaune, croix) — pendant motion/confirming
        if detector.candidate_tip and detector.state in ("motion", "confirming"):
            tp = (int(detector.candidate_tip[0]), int(detector.candidate_tip[1]))
            cv2.drawMarker(out, tp, config.COLOR_YELLOW,
                           cv2.MARKER_CROSS, 22, 2)

        # Tous les tips confirmés (cercles verts)
        for pt in detector.all_detections:
            cv2.circle(out, (int(pt[0]), int(pt[1])), 4, config.COLOR_GREEN, -1)
            cv2.circle(out, (int(pt[0]), int(pt[1])), 6, config.COLOR_WHITE, 1)

        # Dernière détection mise en évidence
        if detector.last_detection:
            ld = (int(detector.last_detection[0]), int(detector.last_detection[1]))
            cv2.circle(out, ld, 8, config.COLOR_GREEN, -1)
            cv2.circle(out, ld, 11, config.COLOR_WHITE, 2)

        # Mini masque de diff en coin haut-droit
        if detector.debug_mask is not None:
            try:
                ms = cv2.resize(detector.debug_mask, (140, 140))
                mc = cv2.cvtColor(ms, cv2.COLOR_GRAY2BGR)
                y1, x1 = 6, out.shape[1] - 146
                if x1 > 0:
                    out[y1:y1+140, x1:x1+140] = mc
                    cv2.rectangle(out, (x1-1, y1-1), (x1+140, y1+140),
                                  config.COLOR_WHITE, 1)
                    cv2.putText(out, "DIFF", (x1+4, y1+14),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.42,
                                config.COLOR_WHITE, 1)
            except Exception:
                pass

        return out


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
        # Override CLI : force le mode int-index sur les N premiers slots
        # (utile en dev sans udev rules, ou pour pointer sur des indexes
        # arbitraires). On clear le device pour empêcher la résolution path.
        config.CAM_INDEXES = args.cams
        for i, idx in enumerate(args.cams):
            if i < len(config.CAM_SLOTS):
                config.CAM_SLOTS[i] = {**config.CAM_SLOTS[i],
                                        "device": None, "index": idx}
        # Si --cams donne plus d'indexes que de slots configurés, on étend
        # avec des slots génériques pour rester souple.
        for i in range(len(config.CAM_SLOTS), len(args.cams)):
            config.CAM_SLOTS.append({
                "slot": chr(ord("A") + i),
                "device": None,
                "index": args.cams[i],
                "master": False,
            })
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
