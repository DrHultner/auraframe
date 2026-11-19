from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .audio_monitor import AudioSample
from .display import TrackInfo

logger = logging.getLogger(__name__)


@dataclass
class RecognitionResult:
    track: TrackInfo
    artwork_url: str | None = None
    provider: str = ""
    raw: dict[str, Any] | None = None


class Recognizer:
    """Cloud track recognition client. Stub for later phases."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._cfg = config.get("recognition", {})
        self._provider = self._cfg.get("provider", "audd")

    def identify(self, sample: AudioSample) -> RecognitionResult | None:
        logger.debug("Recognizer stub ignoring sample %s", sample.path)
        return None
