from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from PIL import Image

logger = logging.getLogger(__name__)

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
_CONTENT_TYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
}


class ArtworkManager:
    """Fetch album artwork and serve it from a local disk cache."""

    def __init__(self, config: dict[str, Any]) -> None:
        art_cfg = config.get("artwork", {})
        self._cache_dir = Path(art_cfg.get("cache_dir", "~/.cache/auraframe/art")).expanduser()
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._max_cache_bytes = int(float(art_cfg.get("max_cache_mb", 500)) * 1024 * 1024)
        self._max_age_s = int(float(art_cfg.get("max_age_days", 30)) * 86400)
        self._timeout_s = float(art_cfg.get("download_timeout_s", 20))
        self._retries = int(art_cfg.get("download_retries", 3))
        self._retry_backoff_s = float(art_cfg.get("retry_backoff_s", 0.75))
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": "AuraFrame/0.1 (+https://github.com/DrHultner/auraframe)",
                "Accept": "image/*,*/*;q=0.8",
            }
        )
        self._last_path: Path | None = None
        logger.info(
            "Artwork cache dir=%s max_mb=%s max_age_days=%s",
            self._cache_dir,
            art_cfg.get("max_cache_mb", 500),
            art_cfg.get("max_age_days", 30),
        )

    @property
    def cache_dir(self) -> Path:
        return self._cache_dir

    @property
    def last_path(self) -> Path | None:
        return self._last_path

    def ensure(self, source: str | Path, _meta: Any = None) -> Path | None:
        """Return a local filesystem path for artwork (download + cache if URL)."""
        if source is None:
            return None
        text = str(source).strip()
        if not text:
            return None

        if self._is_url(text):
            path = self._ensure_url(text)
        else:
            path = self._ensure_local(text)

        if path is not None:
            self._touch(path)
            self._last_path = path
        return path

    def get_cached(self, source: str | Path) -> Path | None:
        """Return cached file for URL/path if present, without downloading."""
        text = str(source).strip()
        if not text:
            return None
        if not self._is_url(text):
            path = Path(text).expanduser()
            return path if path.is_file() else None
        cached = self._find_cached(self._cache_key(text))
        return cached

    def prune(self) -> None:
        """Drop expired and oversize cache entries (oldest first)."""
        files = self._cache_files()
        now = time.time()
        kept: list[Path] = []

        for path in files:
            age = now - path.stat().st_mtime
            if self._max_age_s > 0 and age > self._max_age_s:
                self._unlink(path)
                logger.info("Pruned expired artwork %s", path.name)
            else:
                kept.append(path)

        total = sum(p.stat().st_size for p in kept if p.is_file())
        if self._max_cache_bytes <= 0 or total <= self._max_cache_bytes:
            return

        kept.sort(key=lambda p: p.stat().st_mtime)
        for path in kept:
            if total <= self._max_cache_bytes:
                break
            try:
                size = path.stat().st_size
            except OSError:
                continue
            self._unlink(path)
            total -= size
            logger.info("Pruned artwork for size budget %s", path.name)

    def _ensure_local(self, source: str) -> Path | None:
        path = Path(source).expanduser()
        if path.is_file():
            return path.resolve()
        logger.warning("Artwork not found: %s", source)
        return None

    def _ensure_url(self, url: str) -> Path | None:
        key = self._cache_key(url)
        cached = self._find_cached(key)
        if cached is not None:
            logger.debug("Artwork cache hit %s", cached.name)
            return cached

        self.prune()
        try:
            path = self._download(url, key)
        except Exception:
            logger.exception("Artwork download failed: %s", url)
            return None

        self.prune()
        return path

    def _download(self, url: str, key: str) -> Path:
        last_error: Exception | None = None
        for attempt in range(1, self._retries + 1):
            try:
                return self._download_once(url, key)
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Artwork download attempt %s/%s failed: %s",
                    attempt,
                    self._retries,
                    exc,
                )
                if attempt < self._retries:
                    time.sleep(self._retry_backoff_s * attempt)
        assert last_error is not None
        raise last_error

    def _download_once(self, url: str, key: str) -> Path:
        logger.info("Downloading artwork %s", url)
        with self._session.get(url, timeout=self._timeout_s, stream=True) as resp:
            resp.raise_for_status()
            content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            ext = self._guess_extension(url, content_type)
            dest = self._cache_dir / f"{key}{ext}"
            tmp_fd, tmp_name = tempfile.mkstemp(prefix=f".{key}.", suffix=ext, dir=self._cache_dir)
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(tmp_fd, "wb") as out:
                    for chunk in resp.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            out.write(chunk)
                self._validate_image(tmp_path)
                os.replace(tmp_path, dest)
            except Exception:
                self._unlink(tmp_path)
                raise

        logger.info("Cached artwork %s (%s bytes)", dest.name, dest.stat().st_size)
        return dest

    def _find_cached(self, key: str) -> Path | None:
        matches = sorted(self._cache_dir.glob(f"{key}.*"))
        for path in matches:
            if path.is_file() and not path.name.startswith("."):
                return path
        return None

    def _cache_files(self) -> list[Path]:
        return [
            p
            for p in self._cache_dir.iterdir()
            if p.is_file() and not p.name.startswith(".")
        ]

    @staticmethod
    def _cache_key(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    @staticmethod
    def _is_url(value: str) -> bool:
        parsed = urlparse(value)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)

    @staticmethod
    def _guess_extension(url: str, content_type: str) -> str:
        if content_type in _CONTENT_TYPE_EXT:
            return _CONTENT_TYPE_EXT[content_type]
        path = urlparse(url).path
        ext = Path(path).suffix.lower()
        if ext in _IMAGE_EXTENSIONS:
            return ".jpg" if ext == ".jpeg" else ext
        guessed = mimetypes.guess_extension(content_type or "") or ".jpg"
        if guessed == ".jpe":
            return ".jpg"
        return guessed if guessed in _IMAGE_EXTENSIONS else ".jpg"

    @staticmethod
    def _validate_image(path: Path) -> None:
        with Image.open(path) as img:
            img.verify()

    @staticmethod
    def _touch(path: Path) -> None:
        try:
            now = time.time()
            os.utime(path, (now, now))
        except OSError:
            pass

    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.debug("Could not remove %s", path, exc_info=True)
