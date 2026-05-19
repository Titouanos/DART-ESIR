"""
DartVision - Game Engine
Supports: Free Play, 501, 301 with standard rules.
"""

from dataclasses import dataclass, field
from typing import Callable, List, Optional


@dataclass
class Throw:
    label: str
    score: int
    number: int
    multiplier: int
    ring: str


@dataclass
class Turn:
    """A turn = up to 3 throws."""
    throws: List[Throw] = field(default_factory=list)
    busted: bool = False

    @property
    def total(self) -> int:
        if self.busted:
            return 0
        return sum(t.score for t in self.throws)

    @property
    def is_complete(self) -> bool:
        return len(self.throws) >= 3 or self.busted


class Player:
    def __init__(self, name: str, start_score: int = 0):
        self.name = name
        self.start_score = start_score
        self.score = start_score
        self.turns: List[Turn] = []
        self.current_turn: Turn = Turn()
        # Stats étendues (incrémentées dans GameEngine.register_throw)
        self.one_eighty: int = 0
        self.tons_plus: int = 0           # tours >= 100 hors 180
        self.best_turn: int = 0
        self.busts: int = 0
        self.checkout_attempts: int = 0   # tours où le joueur tentait un finish double
        self.checkout_success: int = 0
        self.high_checkout: int = 0       # plus gros checkout réussi

    @property
    def darts_in_turn(self) -> int:
        return len(self.current_turn.throws)

    @property
    def darts_total(self) -> int:
        return sum(len(t.throws) for t in self.turns) + self.darts_in_turn

    @property
    def avg_per_turn(self) -> float:
        completed = [t for t in self.turns if not t.busted]
        if not completed:
            return 0.0
        return sum(t.total for t in completed) / len(completed)

    @property
    def checkout_pct(self) -> int:
        if self.checkout_attempts == 0:
            return 0
        return round(self.checkout_success * 100 / self.checkout_attempts)

    def current_turn_chips(self) -> List[Optional[str]]:
        """Retourne 3 labels (ou None pour les non-tirées) du tour en cours."""
        chips: List[Optional[str]] = [None, None, None]
        for i, throw in enumerate(self.current_turn.throws[:3]):
            chips[i] = throw.label
        return chips

    def checkout_routes(self) -> List[str]:
        """Simple checkout suggestions for scores <= 170."""
        s = self.score
        if s > 170 or s <= 1:
            return []
        routes = []
        if s == 50:
            routes.append("BULL")
        elif s <= 40 and s % 2 == 0:
            routes.append(f"D{s // 2}")
        elif s <= 60 and s > 40:
            routes.append(f"S{s - 40} → D20")
        return routes


class GameEngine:
    """
    Manages game state for one or more players.

    Observer pattern : tout changement métier émet une notification via
    `_notify(event_type, payload)`. Les abonnés enregistrés via `subscribe()`
    sont appelés synchrone dans le thread courant ; le bridge web s'en sert
    pour pousser les events WS. Source unique de vérité : tout passe par
    GameEngine, qu'on vienne du clavier OpenCV ou du WebSocket.
    """

    MODES = ["free", "501", "301"]

    def __init__(self, mode: str = "free", player_names: List[str] = None):
        # Abonnés à instaurer AVANT toute mutation, pour pouvoir notifier
        # un éventuel "game_reset" lors d'un reset() qui réutilise le ctor.
        self._subscribers: List[Callable[[str, dict], None]] = []
        self._init_state(mode, player_names)

    # -----------------------------------------------------------------
    # OBSERVER
    # -----------------------------------------------------------------
    def subscribe(self, callback: Callable[[str, dict], None]) -> None:
        """Enregistre un callback `(event_type: str, payload: dict) -> None`."""
        if callback not in self._subscribers:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[str, dict], None]) -> None:
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def _notify(self, event_type: str, payload: dict) -> None:
        """Diffuse un event aux abonnés ; un abonné qui crash n'impacte pas les autres."""
        for cb in list(self._subscribers):
            try:
                cb(event_type, payload)
            except Exception as e:
                # On ne casse jamais la boucle de jeu pour un abonné défaillant.
                # Le bridge web logue lui-même ses erreurs internes.
                print(f"[GameEngine] subscriber error on '{event_type}': {e}")

    # -----------------------------------------------------------------
    # INIT / RESET
    # -----------------------------------------------------------------
    def _init_state(self, mode: str, player_names: Optional[List[str]]) -> None:
        self.mode = mode if mode in self.MODES else "free"
        names = player_names or ["Player 1"]
        start_score = 501 if self.mode == "501" else (301 if self.mode == "301" else 0)
        self.players = [Player(n, start_score) for n in names]
        self.current_player_idx = 0
        self.game_over = False
        self.winner: Optional[str] = None
        self.history: List[dict] = []
        self.turn_number = 1  # numéro de volée global (incrémente quand player 0 commence un tour)

    def reset(self, mode: Optional[str] = None,
              player_names: Optional[List[str]] = None,
              keep_players: bool = True) -> None:
        """Recrée le state initial.

        - reset() seul → mêmes joueurs/mode, scores remis à zéro.
        - reset(mode="501", player_names=[...]) → nouvelle partie complète.
        - keep_players=True (défaut) → conserve les noms actuels si player_names absent.
        """
        if mode is None:
            mode = self.mode
        if player_names is None and keep_players:
            player_names = [p.name for p in self.players]
        self._init_state(mode, player_names)
        self._notify("game_reset", {"mode": self.mode,
                                     "players": [p.name for p in self.players]})

    @property
    def current_player(self) -> Player:
        return self.players[self.current_player_idx]

    # -----------------------------------------------------------------
    # SNAPSHOT
    # -----------------------------------------------------------------
    def get_full_state(self) -> dict:
        """Snapshot complet — utilisé pour l'event WS `snapshot`."""
        return {
            "mode": self.mode,
            "leg": {"current": 1, "total": 1},  # single-leg, structure prête multi-leg
            "current_player": self.current_player_idx,
            "turn_number": self.turn_number,
            "dart_in_turn": self.current_player.darts_in_turn,
            "game_over": self.game_over,
            "winner": self.winner,
            "players": [
                {
                    "pid": i,
                    "name": p.name,
                    "score": p.score,
                    "active": i == self.current_player_idx,
                    "avg_turn": round(p.avg_per_turn, 1),
                    "darts_in_turn": p.darts_in_turn,
                    "darts_total": p.darts_total,
                    "turns": len(p.turns),
                    "current_throws": p.current_turn_chips(),
                    "checkout": p.checkout_routes() if self.mode in ("501", "301") else [],
                    "checkout_pct": p.checkout_pct,
                    "high_checkout": p.high_checkout,
                    "best_turn": p.best_turn,
                    "one_eighty": p.one_eighty,
                    "tons_plus": p.tons_plus,
                    "busts": p.busts,
                }
                for i, p in enumerate(self.players)
            ],
            "history": self.history[-30:],   # garde un historique de 30 lancers max
        }

    def register_throw(self, score_data: dict) -> dict:
        """
        Register a throw and update game state.

        Args:
            score_data: dict from board.compute_score()

        Returns:
            dict with result info
        """
        player = self.current_player
        player_pid = self.current_player_idx
        throw = Throw(
            label=score_data["label"],
            score=score_data["score"],
            number=score_data["number"],
            multiplier=score_data["multiplier"],
            ring=score_data["ring"],
        )

        # Le tour vient-il d'une tentative de checkout ? (X01 uniquement)
        was_checkout_attempt = self.mode in ("501", "301") and player.score <= 170

        result = {
            "player": player.name,
            "throw": throw,
            "bust": False,
            "turn_complete": False,
            "game_over": False,
            "winner": None,
        }

        # --- Free play mode ---
        if self.mode == "free":
            player.current_turn.throws.append(throw)
            player.score += throw.score
            if player.current_turn.is_complete:
                result["turn_complete"] = True

        # --- X01 modes ---
        elif self.mode in ("501", "301"):
            new_score = player.score - throw.score

            # Bust check: can't go below 0, and must finish on a double
            if new_score < 0 or (new_score == 0 and throw.multiplier != 2
                                  and throw.ring != "double_bull"):
                # BUST - revert entire turn
                player.current_turn.busted = True
                player.current_turn.throws.append(throw)
                player.score += sum(t.score for t in player.current_turn.throws[:-1])
                result["bust"] = True
                result["turn_complete"] = True

            elif new_score == 1:
                # Can't finish on 1 in double-out
                player.current_turn.busted = True
                player.current_turn.throws.append(throw)
                player.score += sum(t.score for t in player.current_turn.throws[:-1])
                result["bust"] = True
                result["turn_complete"] = True

            elif new_score == 0:
                # GAME WON
                player.score = 0
                player.current_turn.throws.append(throw)
                self.game_over = True
                self.winner = player.name
                result["game_over"] = True
                result["winner"] = player.name
                result["turn_complete"] = True

            else:
                player.score = new_score
                player.current_turn.throws.append(throw)
                if player.current_turn.is_complete:
                    result["turn_complete"] = True

        # Log
        self.history.append({
            "player": player.name,
            "label": throw.label,
            "score": throw.score,
        })

        # Notify: throw broadcasté APRÈS mise à jour de l'état, avant turn_end
        self._notify("throw", {
            "pid": player_pid,
            "player": player.name,
            "throw_index": len(player.current_turn.throws) - 1,
            "label": throw.label,
            "number": throw.number,
            "multiplier": throw.multiplier,
            "ring": throw.ring,
            "score": throw.score,
            "remaining": player.score,
            "fusion": score_data.get("fusion") or {
                "cams": score_data.get("fusion_cams", []),
                "confidence": score_data.get("fusion_confidence", 0.0),
                "method": score_data.get("fusion_method", "single"),
            },
        })

        # Bust → notification dédiée AVANT _end_turn (next_player change ensuite)
        if result["bust"]:
            next_pid = (player_pid + 1) % len(self.players)
            self._notify("bust", {
                "pid": player_pid,
                "player": player.name,
                "score_left": player.score,
                "next_pid": next_pid,
                "next_name": self.players[next_pid].name,
            })

        # Tour terminé → finalisation + (sauf game_over qui fige la partie)
        if result["turn_complete"]:
            self._end_turn(was_checkout_attempt=was_checkout_attempt,
                           winning_turn=result["game_over"],
                           advance=not result["game_over"])
            if result["game_over"]:
                self._notify("game_over", self._build_game_over_payload())

        return result

    def _pending_total(self, player: Player) -> int:
        """Total du tour courant (avant qu'il soit poussé dans turns)."""
        return 0 if player.current_turn.busted else sum(t.score for t in player.current_turn.throws)

    def _finalize_turn_stats(self, player: Player, was_checkout_attempt: bool,
                              winning_turn: bool = False) -> int:
        """Met à jour les compteurs de stats du tour courant. Retourne le total."""
        total = self._pending_total(player)
        if player.current_turn.busted:
            player.busts += 1
        else:
            if total == 180:
                player.one_eighty += 1
            elif total >= 100:
                player.tons_plus += 1
            if total > player.best_turn:
                player.best_turn = total
        if was_checkout_attempt:
            player.checkout_attempts += 1
            if winning_turn:
                player.checkout_success += 1
                if total > player.high_checkout:
                    player.high_checkout = total
        return total

    def _end_turn(self, was_checkout_attempt: bool = False,
                  winning_turn: bool = False, advance: bool = True):
        """Finalise stats, pousse le tour, émet turn_end, et avance (si advance=True)."""
        prev_player = self.current_player
        prev_pid = self.current_player_idx
        total = self._finalize_turn_stats(prev_player, was_checkout_attempt, winning_turn)
        busted = prev_player.current_turn.busted
        prev_player.turns.append(prev_player.current_turn)
        prev_player.current_turn = Turn()

        self._notify("turn_end", {
            "pid": prev_pid,
            "player": prev_player.name,
            "total": total,
            "busted": busted,
            "next_pid": (prev_pid + 1) % len(self.players) if advance else prev_pid,
        })

        if not advance:
            return

        self.current_player_idx = (self.current_player_idx + 1) % len(self.players)
        if self.current_player_idx == 0:
            self.turn_number += 1
        self._notify("player_change", {
            "pid": self.current_player_idx,
            "name": self.current_player.name,
        })

    def _build_game_over_payload(self) -> dict:
        """Snapshot complet pour l'event game_over (utilisé par end.html)."""
        ranked = sorted(self.players, key=lambda p: (p.score, -p.darts_total))
        # En X01 : le vainqueur a 0, les autres triés par score restant ascendant.
        # En free : le plus gros score gagne → on inverse.
        if self.mode == "free":
            ranked = sorted(self.players, key=lambda p: -p.score)
        labels = ["vainqueur", "2ᵉ place", "3ᵉ place", "4ᵉ place",
                  "5ᵉ place", "6ᵉ place", "7ᵉ place", "8ᵉ place"]
        winner = next((p for p in self.players if p.name == self.winner), self.players[0])
        # "finish" = labels des 3 dernières fléchettes du tour gagnant
        finish_chain = []
        if winner.turns:
            finish_chain = [t.label for t in winner.turns[-1].throws[-3:]]
        return {
            "winner_pid": self.players.index(winner),
            "winner_name": winner.name,
            "finish": " → ".join(finish_chain) if finish_chain else "",
            "darts": winner.darts_total,
            "turns": len(winner.turns),
            "avg": round(winner.avg_per_turn, 1),
            "best": winner.best_turn,
            "busts": winner.busts,
            "podium": [
                {"pid": self.players.index(p), "name": p.name,
                 "score": p.score, "label": labels[i] if i < len(labels) else f"{i+1}ᵉ"}
                for i, p in enumerate(ranked)
            ],
        }

    def undo_last_throw(self) -> bool:
        """Undo the last registered throw. Returns True if successful.

        Note : on inverse aussi les stats de tour (busts, best_turn, etc.) si
        on remonte au tour précédent. Pour l'instant on garde une logique simple
        et on émet `state_changed` pour forcer un re-snapshot côté UI plutôt
        que de tracer toutes les déltas.
        """
        player = self.current_player
        rewound_turn = False  # True si on remonte au tour précédent

        if not player.current_turn.throws:
            prev_idx = (self.current_player_idx - 1) % len(self.players)
            prev_player = self.players[prev_idx]
            if not prev_player.turns:
                return False
            prev_player.current_turn = prev_player.turns.pop()
            # Si le tour qu'on restaure était busté, on annule l'incrément de bust.
            if prev_player.current_turn.busted:
                prev_player.busts = max(0, prev_player.busts - 1)
            self.current_player_idx = prev_idx
            # Si on revient sur le joueur 0 depuis le dernier, on décrémente le numéro de volée
            if self.current_player_idx == len(self.players) - 1:
                self.turn_number = max(1, self.turn_number - 1)
            player = prev_player
            rewound_turn = True

        if not player.current_turn.throws:
            return False

        throw = player.current_turn.throws.pop()

        if self.mode == "free":
            player.score -= throw.score
        elif self.mode in ("501", "301"):
            player.score += throw.score

        if self.history:
            self.history.pop()

        player.current_turn.busted = False
        self.game_over = False
        self.winner = None

        # Notification : un undo peut toucher le current_player, la partie game_over,
        # les stats... le plus sûr est de demander un re-snapshot complet.
        self._notify("state_changed", {"cause": "undo",
                                        "rewound_turn": rewound_turn})
        return True

    def force_next_turn(self):
        """Manually end the current turn early (e.g., dart fell out)."""
        if self.game_over:
            return
        # Pas de tentative de checkout sur un tour forcé (le joueur n'a pas tiré sa 3e fléchette).
        self._end_turn(was_checkout_attempt=False, winning_turn=False, advance=True)

    def get_scoreboard(self) -> List[dict]:
        """Get current scoreboard data."""
        board = []
        for i, p in enumerate(self.players):
            board.append({
                "name": p.name,
                "score": p.score,
                "darts": p.darts_in_turn,
                "avg": round(p.avg_per_turn, 1),
                "turns": len(p.turns),
                "active": i == self.current_player_idx,
                "checkout": p.checkout_routes() if self.mode in ("501", "301") else [],
            })
        return board
