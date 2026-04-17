"""Playback intent tracking for optimistic UI updates.

Bridges the gap between instant commands and slow state polling.
"""

import time
from dataclasses import dataclass


@dataclass
class PlaybackIntent:
    """A pending user intent for playback state.

    Attributes:
        target_state: The MediaPlayerState we expect after the command.
        command: The service/command name (for retries and logging).
        created_at: Monotonic timestamp when the intent was created.
        prior_state: The source state before the intent (for detecting external overrides).
        retry_count: Number of retries attempted so far.
    """

    target_state: str  # MediaPlayerState value (e.g. "playing", "paused")
    command: str
    created_at: float
    prior_state: str | None = None
    retry_count: int = 0

    @classmethod
    def create(cls, target_state: str, command: str, prior_state: str | None = None) -> "PlaybackIntent":
        return cls(
            target_state=target_state,
            command=command,
            created_at=time.monotonic(),
            prior_state=prior_state,
        )

    @property
    def age_seconds(self) -> float:
        return round(time.monotonic() - self.created_at, 1)

    def to_debug_dict(self) -> dict:
        """Serialize for the _intent debug attribute."""
        return {
            "target": self.target_state,
            "command": self.command,
            "retries": self.retry_count,
            "age_s": self.age_seconds,
        }
