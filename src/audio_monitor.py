from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AudioSample:
    path: str
    duration_s: float


class AudioMonitor:
    """Continuous capture + music gate. Stub for later phases."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._cfg = config.get("audio", {})
        self._enabled = False

    def start(self) -> None:
        self._enabled = True
        logger.info("AudioMonitor stub started (capture not implemented yet)")

    def stop(self) -> None:
        self._enabled = False
        logger.info("AudioMonitor stopped")

    def poll(self) -> AudioSample | None:
        return None
