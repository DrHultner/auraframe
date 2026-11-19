from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ArtworkManager:
    """Fetch and cache album artwork. Phase 1: local path passthrough."""

    def __init__(self, config: dict[str, Any]) -> None:
        art_cfg = config.get("artwork", {})
        self._cache_dir = Path(art_cfg.get("cache_dir", "~/.cache/auraframe/art")).expanduser()
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Artwork cache dir %s", self._cache_dir)

    def ensure(self, source: str | Path, _meta: Any = None) -> Path | None:
        path = Path(source).expanduser()
        if path.is_file():
            return path
        logger.warning("Artwork not found: %s", source)
        return None

    @property
    def cache_dir(self) -> Path:
        return self._cache_dir
