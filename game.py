"""
DartVision - Game Engine
Supports: Free Play, 501, 301 with standard rules.
"""

import uuid
from dataclasses import dataclass, field
from typing import Callable, List, Optional


def _double_label(rem: int) -> str:
    """Label du double qui ferme `rem` (rem pair 2..40, ou 50 = double bull)."""
    return "BULL" if rem == 50 else f"D{rem // 2}"


def _is_double_finish(rem: int) -> bool:
    """`rem` peut-il être fermé par UNE fléchette double-out ?"""
    return rem == 50 or (2 <= rem <= 40 and rem % 2 == 0)


def _scoring_throws():
    """Fléchettes amont possibles (hors finition), des plus 'propres' aux moins
    courantes : gros triples d'abord → routes proches des finitions standard."""
    opts = [(3 * n, f"T{n}") for n in range(20, 0, -1)]
    opts.append((50, "BULL"))
    opts += [(2 * n, f"D{n}") for n in range(20, 0, -1)]
    opts.append((25, "25"))
    opts += [(n, f"S{n}") for n in range(20, 0, -1)]
    return opts


_SCORING_THROWS = _scoring_throws()

# Confort des doubles de finition (plus haut = préféré) : on suit la logique
# usuelle D20 → D16 → D8 → D4 → D2 (route 'halving'), puis bull et les doubles
# pairs courants. Sert à choisir LA route 2 fléchettes la plus naturelle.
_DOUBLE_RANK = {40: 100, 32: 95, 16: 90, 8: 85, 4: 80, 2: 75, 50: 70,
                24: 65, 20: 64, 36: 62, 28: 60, 12: 58}


def _double_rank(rem: int) -> int:
    return _DOUBLE_RANK.get(rem, 40 if (rem // 2) % 2 == 0 else 20)


def compute_checkout(score: int):
    """UNE route de checkout (liste de labels) pour `score`, ou [] si hors de
    2..170 ou non finissable en double-out (bogey : 159/162/163/165/166/168/169).

    Routes valides et proches des finitions usuelles. Ce n'est pas toujours la
    route 'pro' canonique mais une fermeture légale en ≤ 3 fléchettes terminée
    par un double."""
    if score < 2 or score > 170:
        return []
    if _is_double_finish(score):                      # 1 fléchette
        return [_double_label(score)]
    # 2 fléchettes : amont SIMPLE ou TRIPLE d'abord (on ne joue pas un double/
    # bull comme fléchette de mise en place : trop risqué), puis on maximise le
    # confort du double de finition. -> 60: S20 D20 · 80: T16 D16 · 70: T10 D20.
    first_class = {"S": 2, "T": 2, "B": 1, "2": 1, "D": 0}  # 1re lettre du label
    best, best_key = None, (-1, -1)
    for v1, l1 in _SCORING_THROWS:
        rem = score - v1
        if _is_double_finish(rem):
            key = (first_class.get(l1[0], 0), _double_rank(rem))
            if key > best_key:
                best, best_key = [l1, _double_label(rem)], key
    if best is not None:
        return best
    for v1, l1 in _SCORING_THROWS:                    # 3 fléchettes (T20 d'abord)
        for v2, l2 in _SCORING_THROWS:
            if _is_double_finish(score - v1 - v2):
                return [l1, l2, _double_label(score - v1 - v2)]
    return []


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
        """Route de checkout (liste de labels) pour le score courant, ou []."""
        return compute_checkout(self.score)


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
        # Identifiant unique régénéré à chaque nouvelle partie (setup/reset/boot).
        # Le frontend s'en sert pour détecter "j'ai perdu mon état" après une déco
        # suivie d'un redémarrage serveur (game_id différent au snapshot post-reco).
        self.game_id = uuid.uuid4().hex[:8]

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
            "game_id": self.game_id,
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
        # Partie terminée : on ignore tout lancer résiduel (faux positif ou
        # fléchette relancée avant le reset). Sans ça, en X01 le score du
        # vainqueur (0) part en négatif → chemin "bust" → events throw/bust/
        # turn_end fantômes et stats corrompues APRÈS la victoire.
        if self.game_over:
            noop = Throw(label="-", score=0, number=0, multiplier=0, ring="")
            return {"player": self.current_player.name, "throw": noop,
                    "bust": False, "turn_complete": False,
                    "game_over": True, "winner": self.winner, "ignored": True}

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
        nxt = self.current_player
        self._notify("player_change", {
            "pid": self.current_player_idx,
            "name": nxt.name,
            # Score restant + route de checkout : permet l'annonce vocale du
            # finish et un éventuel affichage au changement de joueur.
            "score": nxt.score,
            "checkout": nxt.checkout_routes() if self.mode in ("501", "301") else [],
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

    def _recompute_score(self, player) -> None:
        """Recalcule player.score depuis l'historique des tours, source de
        vérité. Turn.total renvoie 0 pour un tour busté, donc un tour annulé
        ne compte pas. Évite les dérives de l'ajustement incrémental."""
        scored = sum(t.total for t in player.turns) + player.current_turn.total
        if self.mode == "free":
            player.score = scored
        else:  # 301 / 501 : on décompte depuis le score de départ
            player.score = player.start_score - scored

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

        player.current_turn.throws.pop()

        if self.history:
            self.history.pop()

        # Retirer le lancer qui a fait sauter le tour réhabilite ce tour.
        player.current_turn.busted = False
        self.game_over = False
        self.winner = None

        # Recalcule le score depuis l'historique des tours (source de vérité).
        # L'ancien `score += throw.score` était FAUX après un bust : le tour
        # busté n'avait rien soustrait (score déjà revenu au début du tour),
        # donc rajouter le lancer gonflait le score (8 → 21 au lieu de 8).
        self._recompute_score(player)

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
