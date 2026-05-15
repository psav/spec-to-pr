from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

_NO_PROGRESS_THRESHOLD = 0.05  # progress score delta considered "no progress"


class TripReason(str, Enum):
    MAX_ATTEMPTS_REACHED = "max_attempts_reached"
    REPEATED_ERROR = "repeated_error"
    NO_PROGRESS = "no_progress"


@dataclass
class CircuitBreaker:
    max_attempts: int = 3
    error_fingerprints: list[str] = field(default_factory=list)
    progress_scores: list[float] = field(default_factory=list)
    tripped: bool = False
    trip_reason: Optional[TripReason] = None

    @property
    def attempt_count(self) -> int:
        return len(self.error_fingerprints)

    def record_attempt(self, error_fingerprint: str, progress_score: float) -> None:
        self.error_fingerprints.append(error_fingerprint)
        self.progress_scores.append(progress_score)
        self._evaluate()

    def _evaluate(self) -> None:
        if self.tripped:
            return

        if self.attempt_count >= self.max_attempts:
            self.tripped = True
            self.trip_reason = TripReason.MAX_ATTEMPTS_REACHED
            return

        # Repeated identical error across two consecutive attempts
        if len(self.error_fingerprints) >= 2:
            if self.error_fingerprints[-1] == self.error_fingerprints[-2]:
                self.tripped = True
                self.trip_reason = TripReason.REPEATED_ERROR
                return

        # No measurable progress between two consecutive attempts
        if len(self.progress_scores) >= 2:
            delta = abs(self.progress_scores[-1] - self.progress_scores[-2])
            if delta < _NO_PROGRESS_THRESHOLD:
                self.tripped = True
                self.trip_reason = TripReason.NO_PROGRESS
                return
