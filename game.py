"""
DartVision - Game Engine
Supports: Free Play, 501, 301 with standard rules.
"""

from dataclasses import dataclass, field
from typing import List, Optional


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

    @property
    def darts_in_turn(self) -> int:
        return len(self.current_turn.throws)

    @property
    def avg_per_turn(self) -> float:
        completed = [t for t in self.turns if not t.busted]
        if not completed:
            return 0.0
        return sum(t.total for t in completed) / len(completed)

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
    """

    MODES = ["free", "501", "301"]

    def __init__(self, mode: str = "free", player_names: List[str] = None):
        self.mode = mode
        player_names = player_names or ["Player 1"]
        start_score = 0
        if mode == "501":
            start_score = 501
        elif mode == "301":
            start_score = 301

        self.players = [Player(name, start_score) for name in player_names]
        self.current_player_idx = 0
        self.game_over = False
        self.winner: Optional[str] = None
        self.history: List[dict] = []  # Full throw history

    @property
    def current_player(self) -> Player:
        return self.players[self.current_player_idx]

    def register_throw(self, score_data: dict) -> dict:
        """
        Register a throw and update game state.

        Args:
            score_data: dict from board.compute_score()

        Returns:
            dict with result info
        """
        player = self.current_player
        throw = Throw(
            label=score_data["label"],
            score=score_data["score"],
            number=score_data["number"],
            multiplier=score_data["multiplier"],
            ring=score_data["ring"],
        )

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
                self._end_turn()

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
                self._end_turn()

            elif new_score == 1:
                # Can't finish on 1 in double-out
                player.current_turn.busted = True
                player.current_turn.throws.append(throw)
                player.score += sum(t.score for t in player.current_turn.throws[:-1])
                result["bust"] = True
                result["turn_complete"] = True
                self._end_turn()

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
                    self._end_turn()

        # Log
        self.history.append({
            "player": player.name,
            "label": throw.label,
            "score": throw.score,
        })

        return result

    def _end_turn(self):
        """End current turn and advance to next player."""
        player = self.current_player
        player.turns.append(player.current_turn)
        player.current_turn = Turn()

        # Next player
        self.current_player_idx = (self.current_player_idx + 1) % len(self.players)

    def undo_last_throw(self) -> bool:
        """Undo the last registered throw. Returns True if successful."""
        player = self.current_player
        if not player.current_turn.throws:
            # Go back to previous player's last turn
            prev_idx = (self.current_player_idx - 1) % len(self.players)
            prev_player = self.players[prev_idx]
            if not prev_player.turns:
                return False
            # Restore previous turn
            prev_player.current_turn = prev_player.turns.pop()
            self.current_player_idx = prev_idx
            player = prev_player

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
        return True

    def force_next_turn(self):
        """Manually end the current turn early (e.g., dart fell out)."""
        self._end_turn()

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
