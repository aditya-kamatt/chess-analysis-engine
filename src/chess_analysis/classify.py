"""Move severity classification (PRD 4.4).

Three tiers only, keyed on win-percentage loss rather than centipawn loss:
dropping 100cp at +9.0 is meaningless, dropping 100cp at 0.00 is a real error.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Severity(StrEnum):
    INACCURACY = "inaccuracy"
    MISTAKE = "mistake"
    BLUNDER = "blunder"


@dataclass(frozen=True)
class Thresholds:
    """Win-percentage loss at which each tier begins.

    Kept as a value object so the numbers stay tunable in one place rather than
    inline at the comparison site, and so tests can vary them freely.
    """

    inaccuracy: float = 10.0
    mistake: float = 20.0
    blunder: float = 30.0

    def classify(self, loss: float) -> Severity | None:
        """Severity for a move that gave up `loss` win percent, or None."""
        if loss >= self.blunder:
            return Severity.BLUNDER
        if loss >= self.mistake:
            return Severity.MISTAKE
        if loss >= self.inaccuracy:
            return Severity.INACCURACY
        return None


DEFAULT_THRESHOLDS = Thresholds()
