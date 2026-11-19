from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import requests

from .audio_monitor import AudioSample
from .config import mask_token
from .display import TrackInfo

logger = logging.getLogger(__name__)


@dataclass
class RecognitionResult:
    track: TrackInfo
    artwork_url: str | None = None
    provider: str = ""
    raw: dict[str, Any] | None = None
    cached: bool = False

    @property
    def track_key(self) -> str:
        return f"{self.track.artist.strip().lower()}|{self.track.title.strip().lower()}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "track": asdict(self.track),
            "artwork_url": self.artwork_url,
            "provider": self.provider,
            "raw": self.raw,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, cached: bool = False) -> RecognitionResult:
        track_data = data.get("track") or {}
        return cls(
            track=TrackInfo(
                title=str(track_data.get("title") or ""),
                artist=str(track_data.get("artist") or ""),
                album=str(track_data.get("album") or ""),
            ),
            artwork_url=data.get("artwork_url"),
            provider=str(data.get("provider") or ""),
            raw=data.get("raw"),
            cached=cached,
        )


class Recognizer:
    """Cloud track recognition (AudD) with disk + memory result cache."""

    def __init__(self, config: dict[str, Any]) -> None:
        cfg = config.get("recognition", {})
        self._provider = str(cfg.get("provider", "audd")).lower()
        self._api_url = str(cfg.get("api_url", "https://api.audd.io/"))
        self._token_env = str(cfg.get("api_token_env", "AUDD_API_TOKEN"))
        raw_token = os.getenv(self._token_env) or cfg.get("api_token") or ""
        self._token = str(raw_token).strip().strip('"').strip("'")
        self._return_meta = str(cfg.get("return_meta", "apple_music,spotify"))
        self._timeout_s = float(cfg.get("timeout_s", 15))
        self._cache_ttl_s = float(cfg.get("cache_ttl_s", 3600))
        self._artwork_size = int(cfg.get("artwork_size", 1000))
        cache_dir = cfg.get("cache_dir", "~/.cache/auraframe/recognition")
        self._cache_dir = Path(cache_dir).expanduser()
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        self._session = requests.Session()
        self._memory: dict[str, tuple[float, RecognitionResult]] = {}
        self._last_result: RecognitionResult | None = None

        env_files = config.get("_env_files") or []
        if not self._token:
            logger.warning(
                "No recognition API token (%s). env_files=%s root=%s",
                self._token_env,
                env_files,
                config.get("_root"),
            )
        else:
            logger.info(
                "Recognizer ready provider=%s token=%s env_files=%s cache_dir=%s",
                self._provider,
                mask_token(self._token),
                env_files,
                self._cache_dir,
            )

    @property
    def configured(self) -> bool:
        return bool(self._token)

    @property
    def last_result(self) -> RecognitionResult | None:
        return self._last_result

    def identify(self, sample: AudioSample) -> RecognitionResult | None:
        path = Path(sample.path)
        if not path.is_file():
            logger.error("Sample missing: %s", path)
            return None

        file_key = self._file_digest(path)
        cached = self._cache_get(file_key)
        if cached is not None:
            logger.info(
                "Recognition cache hit %s — %s [%s]",
                cached.track.artist,
                cached.track.title,
                path.name,
            )
            self._last_result = cached
            return cached

        if not self._token:
            logger.error("Cannot identify: missing API token (%s)", self._token_env)
            return None

        if self._provider != "audd":
            logger.error("Unsupported recognition provider: %s", self._provider)
            return None

        try:
            result = self._identify_audd(path)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            logger.error("Recognition HTTP error %s: %s", status, exc)
            return None
        except requests.RequestException:
            logger.exception("Recognition network error")
            return None
        except Exception:
            logger.exception("Recognition failed")
            return None

        if result is None:
            logger.info("No match for sample %s", path.name)
            return None

        self._cache_put(file_key, result)
        self._last_result = result
        logger.info(
            "Recognized %s — %s (album=%r art=%s)",
            result.track.artist,
            result.track.title,
            result.track.album,
            "yes" if result.artwork_url else "no",
        )
        return result

    def _identify_audd(self, path: Path) -> RecognitionResult | None:
        data = {
            "api_token": self._token,
            "return": self._return_meta,
        }
        size = path.stat().st_size
        stats = self._wav_stats(path)
        logger.info(
            "Calling AudD api=%s file=%s bytes=%s duration=%.1fs peak=%.4f rms=%.4f",
            self._api_url,
            path.name,
            size,
            stats.get("duration_s", 0.0),
            stats.get("peak", 0.0),
            stats.get("rms", 0.0),
        )
        with path.open("rb") as fh:
            files = {"file": (path.name, fh, "audio/wav")}
            resp = self._session.post(
                self._api_url,
                data=data,
                files=files,
                timeout=self._timeout_s,
            )
        logger.info("AudD HTTP %s", resp.status_code)
        resp.raise_for_status()
        payload = resp.json()

        if payload.get("status") == "error":
            err = payload.get("error") or payload
            logger.error("AudD error: %s", err)
            return None

        result = payload.get("result")
        if not result:
            logger.info(
                "AudD returned no match (status=%s). Tips: play louder/clearer, "
                "move mic closer, or re-test after normalize with a fresh sample.",
                payload.get("status"),
            )
            return None

        return self._parse_audd_result(result)

    @staticmethod
    def _wav_stats(path: Path) -> dict[str, float]:
        try:
            import wave

            import numpy as np

            with wave.open(str(path), "rb") as wf:
                frames = wf.readframes(wf.getnframes())
                rate = wf.getframerate()
                width = wf.getsampwidth()
                ch = wf.getnchannels()
            if width == 2:
                data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
            else:
                return {}
            if ch > 1:
                data = data.reshape(-1, ch).mean(axis=1)
            return {
                "duration_s": float(data.size / max(rate, 1)),
                "peak": float(np.max(np.abs(data))) if data.size else 0.0,
                "rms": float(np.sqrt(np.mean(np.square(data)))) if data.size else 0.0,
            }
        except Exception:
            logger.debug("Could not analyze wav %s", path, exc_info=True)
            return {}

    def _parse_audd_result(self, result: dict[str, Any]) -> RecognitionResult:
        title = str(result.get("title") or "").strip()
        artist = str(result.get("artist") or "").strip()
        album = str(result.get("album") or "").strip()

        apple = result.get("apple_music") or {}
        if not album:
            album = str(apple.get("albumName") or "").strip()

        artwork_url = self._extract_artwork_url(result)
        return RecognitionResult(
            track=TrackInfo(title=title, artist=artist, album=album),
            artwork_url=artwork_url,
            provider="audd",
            raw=result,
            cached=False,
        )

    def _extract_artwork_url(self, result: dict[str, Any]) -> str | None:
        apple = result.get("apple_music") or {}
        art = apple.get("artwork") or {}
        url = art.get("url")
        if isinstance(url, str) and url:
            size = self._artwork_size
            return url.replace("{w}", str(size)).replace("{h}", str(size))

        spotify = result.get("spotify") or {}
        album = spotify.get("album") or {}
        images = album.get("images") or []
        if images:
            # Prefer largest
            ordered = sorted(
                images,
                key=lambda img: int(img.get("width") or 0),
                reverse=True,
            )
            sp_url = ordered[0].get("url")
            if isinstance(sp_url, str) and sp_url:
                return sp_url
        return None

    def _cache_get(self, key: str) -> RecognitionResult | None:
        now = time.time()
        mem = self._memory.get(key)
        if mem is not None:
            ts, result = mem
            if self._cache_ttl_s <= 0 or now - ts <= self._cache_ttl_s:
                return RecognitionResult.from_dict(result.to_dict(), cached=True)
            self._memory.pop(key, None)

        path = self._cache_dir / f"{key}.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            saved_at = float(data.get("saved_at") or 0)
            if self._cache_ttl_s > 0 and now - saved_at > self._cache_ttl_s:
                path.unlink(missing_ok=True)
                return None
            result = RecognitionResult.from_dict(data.get("result") or {}, cached=True)
            self._memory[key] = (saved_at, result)
            return result
        except Exception:
            logger.debug("Bad recognition cache file %s", path, exc_info=True)
            return None

    def _cache_put(self, key: str, result: RecognitionResult) -> None:
        now = time.time()
        self._memory[key] = (now, result)
        path = self._cache_dir / f"{key}.json"
        payload = {"saved_at": now, "result": result.to_dict()}
        try:
            path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            logger.debug("Could not write recognition cache %s", path, exc_info=True)

    @staticmethod
    def _file_digest(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 64), b""):
                h.update(chunk)
        return h.hexdigest()
