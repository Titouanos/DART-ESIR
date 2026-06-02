"""
DartVision Web Bridge
=====================
Pont entre le code synchrone (boucle OpenCV de main.py, GameEngine de game.py)
et le serveur FastAPI asynchrone (webui/server.py).

Principes :
- Source unique de vérité : seul GameEngine mute l'état métier. Le bridge
  s'abonne via `game.subscribe(...)` et traduit chaque event en frame WS.
  Clavier OpenCV et commandes WS appellent les MÊMES méthodes du GameEngine,
  donc l'UI reste synchro quel que soit le canal d'action.
- Thread-safe : `broadcast()` est appelable depuis n'importe quel thread ;
  on planifie l'envoi sur la loop asyncio via `call_soon_threadsafe`.
- `push_status()` est throttlé à ~2 Hz pour les valeurs continues, mais push
  IMMÉDIATEMENT dès qu'un booléen change (cam OK→KO, calib OK→KO, etc.).
"""

import asyncio
import json
import logging
import threading
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol, Set

from fastapi import WebSocket

from game import GameEngine

logger = logging.getLogger("dartvision.bridge")


class Controller(Protocol):
    """Contrat que `main.py` doit fournir au bridge pour les actions non-game.

    Les actions métier (undo, next_turn, reset…) passent directement par
    `GameEngine`. Mais capture de référence, recalibration et quit touchent
    au pipeline OpenCV : c'est `main.py` qui sait faire, le bridge délègue.
    """

    def capture_reference(self) -> None: ...
    def start_recalibration(self) -> None: ...
    def request_shutdown(self) -> None: ...


# Throttling minimum entre deux pushes `system_status` quand seules
# les valeurs continues changent (fps, latence, résiduel, etc.).
_STATUS_PERIOD_S = 0.5


class Bridge:
    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._subscribers: Set[WebSocket] = set()
        self._sub_lock = threading.Lock()
        self._game: Optional[GameEngine] = None
        self._controller: Optional[Controller] = None

        # Dernières frames JPEG par slot caméra (A/B/C). Servies par /api/cam/*/mjpeg.
        self._frames: Dict[str, bytes] = {}
        # Variante annotée (tip détecté, contour, ray, masque diff) servie par
        # /api/cam/*/debug.mjpeg. Utile quand la détection part en cacahuète.
        self._frames_debug: Dict[str, bytes] = {}
        self._frame_lock = threading.Lock()

        # Throttling system_status
        self._last_status: Dict[str, Any] = {}
        self._last_status_push: float = 0.0
        self._last_status_bools: Dict[str, Any] = {}

    # =================================================================
    # CYCLE DE VIE
    # =================================================================
    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Appelé une fois la loop uvicorn démarrée (depuis server.py startup hook)."""
        self._loop = loop

    def attach_game(self, game: GameEngine) -> None:
        """S'abonne au GameEngine pour relayer ses events vers les WS."""
        if self._game is not None:
            self._game.unsubscribe(self._on_game_event)
        self._game = game
        game.subscribe(self._on_game_event)

    def attach_controller(self, controller: Controller) -> None:
        self._controller = controller

    @property
    def game(self) -> Optional[GameEngine]:
        return self._game

    # =================================================================
    # OBSERVER : GameEngine → WS
    # =================================================================
    def _on_game_event(self, event_type: str, payload: dict) -> None:
        """Appelé synchrone depuis GameEngine ; ré-émis vers tous les WS clients."""
        # `state_changed` (undo, etc.) → snapshot complet pour resynchroniser proprement
        if event_type == "state_changed" or event_type == "game_reset":
            if self._game is not None:
                self._broadcast("snapshot", self._game.get_full_state())
            return
        # Sinon : relai 1-pour-1 (le type WS == le type game)
        self._broadcast(event_type, payload)

    # =================================================================
    # BROADCAST THREAD-SAFE
    # =================================================================
    def _broadcast(self, event_type: str, payload: dict) -> None:
        """Planifie l'envoi d'une frame à tous les WS depuis n'importe quel thread."""
        if self._loop is None or self._loop.is_closed():
            return  # serveur pas encore démarré ou déjà fermé : on drop silencieusement
        message = {"type": event_type, "ts": time.time(), "payload": payload}
        try:
            self._loop.call_soon_threadsafe(self._loop.create_task, self._fan_out(message))
        except RuntimeError:
            # Loop arrêté entre le check et le call_soon : on ignore.
            pass

    async def _fan_out(self, message: dict) -> None:
        text = json.dumps(message, separators=(",", ":"), default=str)
        with self._sub_lock:
            sockets = list(self._subscribers)
        # On envoie séquentiellement : les WS sont peu nombreux (kiosque + mobiles)
        # et asyncio.gather avec return_exceptions=True permettrait du parallèle
        # mais c'est inutile pour 1-5 clients et complique le debug.
        dead: List[WebSocket] = []
        for ws in sockets:
            try:
                await ws.send_text(text)
            except Exception as e:
                logger.debug("WS send failed (%s), client supprimé", e)
                dead.append(ws)
        if dead:
            with self._sub_lock:
                for ws in dead:
                    self._subscribers.discard(ws)

    # =================================================================
    # SUBSCRIBERS (appelés depuis le handler WS)
    # =================================================================
    async def register_subscriber(self, ws: WebSocket) -> None:
        """Accepte une connexion WS, l'enregistre, lui envoie le snapshot initial."""
        await ws.accept()
        with self._sub_lock:
            self._subscribers.add(ws)
        logger.info("WS connecté (%d clients actifs)", len(self._subscribers))
        # Snapshot immédiat pour hydrater n'importe quel écran à la reconnexion.
        if self._game is not None:
            snapshot = {"type": "snapshot", "ts": time.time(),
                        "payload": self._game.get_full_state()}
            try:
                await ws.send_text(json.dumps(snapshot, separators=(",", ":"), default=str))
            except Exception:
                pass
        # Plus le dernier system_status connu, si on en a un.
        if self._last_status:
            try:
                await ws.send_text(json.dumps(
                    {"type": "system_status", "ts": time.time(),
                     "payload": self._last_status},
                    separators=(",", ":"), default=str))
            except Exception:
                pass

    def unregister_subscriber(self, ws: WebSocket) -> None:
        with self._sub_lock:
            self._subscribers.discard(ws)
        logger.info("WS déconnecté (%d clients restants)", len(self._subscribers))

    # =================================================================
    # SYSTEM STATUS — push avec throttling intelligent
    # =================================================================
    def push_status(self, system: dict) -> None:
        """Pousser un dict de santé système.

        Schéma attendu (cf. STEP 1) :
            {"cams": [{"id":"A","ok":bool,"fps":..,"seg":..,"latency_ms":..,"master":bool}, ...],
             "calibration": {"ok":bool, "residual_mm":float},
             "reference":   {"ok":bool, "captured_at":"HH:MM"},
             "game_state":  "idle"|"setup"|"calib"|"live"|"end"}

        Logique :
        - dès qu'un champ booléen change → push immédiat
        - sinon throttling à 2 Hz pour fps / latence / résiduel
        """
        now = time.time()
        self._last_status = system
        bools = self._extract_bools(system)
        bool_changed = bools != self._last_status_bools
        self._last_status_bools = bools

        if bool_changed or (now - self._last_status_push) >= _STATUS_PERIOD_S:
            self._last_status_push = now
            self._broadcast("system_status", system)

    @staticmethod
    def _extract_bools(system: dict) -> dict:
        """Extrait les seuls booléens qu'on surveille pour le push immédiat."""
        return {
            "cams_ok": tuple(bool(c.get("ok")) for c in system.get("cams", [])),
            "calib_ok": bool(system.get("calibration", {}).get("ok")),
            "ref_ok": bool(system.get("reference", {}).get("ok")),
            "game_state": system.get("game_state"),
        }

    # =================================================================
    # FRAMES CAMÉRAS (pour /api/cam/{slot}/mjpeg)
    # =================================================================
    def set_frame(self, slot: str, jpeg_bytes: bytes) -> None:
        """Stocke le dernier JPEG d'un slot cam. Appelé depuis la boucle OpenCV."""
        with self._frame_lock:
            self._frames[slot] = jpeg_bytes

    def get_frame(self, slot: str) -> Optional[bytes]:
        with self._frame_lock:
            return self._frames.get(slot)

    def set_frame_debug(self, slot: str, jpeg_bytes: bytes) -> None:
        """Variante annotée (overlay détection) pour debug visuel."""
        with self._frame_lock:
            self._frames_debug[slot] = jpeg_bytes

    def get_frame_debug(self, slot: str) -> Optional[bytes]:
        with self._frame_lock:
            return self._frames_debug.get(slot)

    # =================================================================
    # COMMANDES CLIENT → ACTIONS
    # =================================================================
    def handle_command(self, command: dict) -> dict:
        """Traite une frame WS client→serveur.

        Retourne un dict d'ack ({"ok": True}) ou d'erreur ({"ok": False, "msg": ...}).
        N'envoie PAS de broadcast ici — c'est GameEngine qui notifie au fil
        de la mutation d'état (source unique de vérité).
        """
        cmd = command.get("type")
        payload = command.get("payload") or {}

        if self._game is None:
            return {"ok": False, "msg": "GameEngine non attaché"}

        try:
            if cmd == "ping":
                return {"ok": True, "msg": "pong"}

            if cmd == "setup_game":
                mode = payload.get("mode", "501")
                players = payload.get("players") or ["Joueur 1"]
                self._game.reset(mode=mode, player_names=players, keep_players=False)
                return {"ok": True}

            if cmd == "start_game":
                # Le snapshot a déjà été poussé par game_reset / setup_game.
                # `start_game` est ici un no-op métier : on garde la commande
                # pour potentiel hook futur (passage d'un état "setup" à "live").
                return {"ok": True}

            if cmd == "undo_throw":
                ok = self._game.undo_last_throw()
                return {"ok": ok, "msg": None if ok else "Rien à annuler"}

            if cmd == "next_turn":
                self._game.force_next_turn()
                return {"ok": True}

            if cmd == "reset_game":
                # Mêmes joueurs/mode, scores remis à zéro.
                self._game.reset()
                return {"ok": True}

            if cmd == "quit_game":
                # Reset minimal + tag d'état pour passer en "setup" (game.html → setup.html).
                self._game.reset()
                return {"ok": True}

            if cmd == "capture_reference":
                if self._controller is None:
                    return {"ok": False, "msg": "Controller non attaché"}
                self._controller.capture_reference()
                return {"ok": True}

            if cmd == "start_recalibration":
                if self._controller is None:
                    return {"ok": False, "msg": "Controller non attaché"}
                self._controller.start_recalibration()
                return {"ok": True}

            if cmd == "set_tuning":
                # Tuning live des seuils de détection — le detector lit ces
                # valeurs de `config.X` à chaque frame, donc le changement est
                # effectif immédiatement, sans restart.
                import config as _cfg
                p = payload or {}
                changed = {}
                if "diff_threshold" in p:
                    _cfg.DIFF_THRESHOLD = max(5, min(200, int(p["diff_threshold"])))
                    changed["diff_threshold"] = _cfg.DIFF_THRESHOLD
                if "min_dart_area" in p:
                    _cfg.MIN_DART_AREA = max(20, min(2000, int(p["min_dart_area"])))
                    changed["min_dart_area"] = _cfg.MIN_DART_AREA
                if "stable_frames" in p:
                    _cfg.STABLE_FRAMES = max(2, min(60, int(p["stable_frames"])))
                    changed["stable_frames"] = _cfg.STABLE_FRAMES
                if "min_elongation" in p:
                    _cfg.MIN_ELONGATION = max(1.0, min(5.0, float(p["min_elongation"])))
                    changed["min_elongation"] = _cfg.MIN_ELONGATION
                logger.info("set_tuning: %s", changed)
                return {"ok": True, "applied": changed}

            return {"ok": False, "msg": f"Commande inconnue: {cmd!r}"}

        except Exception as e:
            logger.exception("Erreur traitement commande %s", cmd)
            return {"ok": False, "msg": str(e)}
