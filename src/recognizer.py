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

# Documented AudD error codes (https://docs.audd.io/ — Common errors)
# API may return ~40 codes; unknown codes still log raw message from payload.
AUDD_ERROR_CODES: dict[int, str] = {
    51: "Napster metadata requested but Napster is discontinued (warning)",
    300: "Fingerprinting error (audio often too small/short or unusable)",
    400: "Audio file too large (standard endpoint max 10 MB)",
    500: "Invalid audio file",
    600: "Incorrect audio URL",
    700: (
        "No file received for recognition "
        "(use multipart/form-data POST to https://api.audd.io/)"
    ),
    900: "Invalid API token",
    901: "No api_token passed and limit reached (obtain a token from the dashboard)",
}


@dataclass(frozen=True)
class AudDError:
    code: int | None
    message: str
    raw: Any = None

    @property
    def known_meaning(self) -> str | None:
        if self.code is None:
            return None
        return AUDD_ERROR_CODES.get(int(self.code))

    def format(self) -> str:
        parts: list[str] = []
        if self.code is not None:
            parts.append(f"#{self.code}")
        if self.message:
            parts.append(self.message)
        meaning = self.known_meaning
        if meaning and meaning not in (self.message or ""):
            parts.append(f"[{meaning}]")
        return " ".join(parts) if parts else repr(self.raw)


@dataclass
class RecognitionResult:
    track: TrackInfo
    artwork_url: str | None = None
    provider: str = ""
    raw: dict[str, Any] | None = None
    cached: bool = False
    timecode_s: float | None = None
    duration_s: float | None = None
    end_margin_s: float = 5.0
    default_remaining_s: float = 180.0

    @property
    def track_key(self) -> str:
        return f"{self.track.artist.strip().lower()}|{self.track.title.strip().lower()}"

    @property
    def remaining_s(self) -> float:
        """Seconds to wait until the track is predicted to end."""
        if self.duration_s is not None and self.timecode_s is not None:
            left = float(self.duration_s) - float(self.timecode_s)
            return max(5.0, left + float(self.end_margin_s))
        if self.duration_s is not None:
            return max(5.0, float(self.duration_s) + float(self.end_margin_s))
        return max(5.0, float(self.default_remaining_s))

    def to_dict(self) -> dict[str, Any]:
        return {
            "track": asdict(self.track),
            "artwork_url": self.artwork_url,
            "provider": self.provider,
            "raw": self.raw,
            "timecode_s": self.timecode_s,
            "duration_s": self.duration_s,
            "end_margin_s": self.end_margin_s,
            "default_remaining_s": self.default_remaining_s,
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
            timecode_s=_as_optional_float(data.get("timecode_s")),
            duration_s=_as_optional_float(data.get("duration_s")),
            end_margin_s=float(data.get("end_margin_s") or 5.0),
            default_remaining_s=float(data.get("default_remaining_s") or 180.0),
        )


def _as_optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_timecode(value: Any) -> float | None:
    """Parse AudD timecode like '02:32' or '1:02:03' to seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    parts = text.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 1:
        return nums[0]
    if len(nums) == 2:
        return nums[0] * 60.0 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600.0 + nums[1] * 60.0 + nums[2]
    return None


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
        self._end_margin_s = float(cfg.get("end_margin_s", 5.0))
        self._default_remaining_s = float(cfg.get("default_remaining_s", 180.0))
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
            self._log_http_error(exc)
            return None
        except requests.RequestException:
            logger.exception("Recognition network error")
            return None
        except Exception:
            logger.exception("Recognition failed")
            return None

        if result is None:
            # Detailed reason already logged inside _identify_audd
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

        payload = self._parse_payload(resp)
        if payload is None:
            resp.raise_for_status()
            logger.error("AudD returned non-JSON body: %s", resp.text[:500])
            return None

        # HTTP error with JSON body still carries AudD error codes
        if resp.status_code >= 400:
            err = self._extract_error(payload)
            logger.error(
                "AudD HTTP %s with API error %s payload=%s",
                resp.status_code,
                err.format(),
                self._brief_payload(payload),
            )
            return None

        self._log_warnings(payload)

        status = str(payload.get("status") or "").lower()
        if status == "error":
            err = self._extract_error(payload)
            logger.error(
                "AudD API error %s payload=%s",
                err.format(),
                self._brief_payload(payload),
            )
            return None

        if status and status != "success":
            logger.warning(
                "AudD unexpected status=%r payload=%s",
                status,
                self._brief_payload(payload),
            )

        result = payload.get("result")
        if result is None or result == [] or result == {}:
            logger.info(
                "AudD no match for %s (status=%s result=%r). "
                "Not an API error — fingerprint found no song. "
                "Try louder/clearer audio or mic closer to the source.",
                path.name,
                status or "?",
                result,
            )
            return None

        if not isinstance(result, dict):
            logger.error(
                "AudD unexpected result type=%s payload=%s",
                type(result).__name__,
                self._brief_payload(payload),
            )
            return None

        return self._parse_audd_result(result)

    def _log_http_error(self, exc: requests.HTTPError) -> None:
        resp = exc.response
        status = resp.status_code if resp is not None else "?"
        if resp is None:
            logger.error("Recognition HTTP error %s: %s", status, exc)
            return
        payload = self._parse_payload(resp)
        if payload is not None:
            err = self._extract_error(payload)
            logger.error(
                "Recognition HTTP %s AudD error %s payload=%s",
                status,
                err.format(),
                self._brief_payload(payload),
            )
            return
        logger.error(
            "Recognition HTTP %s: %s body=%s",
            status,
            exc,
            (resp.text or "")[:500],
        )

    def _log_warnings(self, payload: dict[str, Any]) -> None:
        warnings = payload.get("warnings")
        if not warnings:
            # Some responses nest a single warning object
            maybe = payload.get("warning")
            if maybe:
                warnings = [maybe]
        if not warnings:
            return
        if not isinstance(warnings, list):
            warnings = [warnings]
        for item in warnings:
            err = self._extract_error({"error": item} if not isinstance(item, dict) or "error_code" in item or "error_message" in item else item)
            # Prefer direct parse when item is already error-shaped
            if isinstance(item, dict):
                err = self._coerce_error_dict(item)
            logger.warning("AudD warning %s", err.format())

    @staticmethod
    def _parse_payload(resp: requests.Response) -> dict[str, Any] | None:
        try:
            data = resp.json()
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    @classmethod
    def _extract_error(cls, payload: dict[str, Any]) -> AudDError:
        err_obj = payload.get("error")
        if isinstance(err_obj, dict):
            return cls._coerce_error_dict(err_obj)
        if isinstance(err_obj, str):
            return AudDError(code=None, message=err_obj, raw=err_obj)
        if isinstance(err_obj, int):
            return AudDError(
                code=err_obj,
                message=AUDD_ERROR_CODES.get(err_obj, ""),
                raw=err_obj,
            )
        # Flat payload variants
        if "error_code" in payload or "error_message" in payload:
            return cls._coerce_error_dict(payload)
        return AudDError(code=None, message=str(err_obj or payload.get("status") or "unknown"), raw=payload)

    @staticmethod
    def _coerce_error_dict(data: dict[str, Any]) -> AudDError:
        code_raw = data.get("error_code", data.get("code", data.get("errorCode")))
        code: int | None
        try:
            code = int(code_raw) if code_raw is not None and str(code_raw).strip() != "" else None
        except (TypeError, ValueError):
            code = None
        message = str(
            data.get("error_message")
            or data.get("message")
            or data.get("errorMessage")
            or ""
        ).strip()
        if not message and code is not None:
            message = AUDD_ERROR_CODES.get(code, "")
        return AudDError(code=code, message=message, raw=data)

    @staticmethod
    def _brief_payload(payload: dict[str, Any]) -> str:
        try:
            text = json.dumps(payload, ensure_ascii=True, default=str)
        except TypeError:
            text = repr(payload)
        if len(text) > 800:
            return text[:800] + "..."
        return text

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
        timecode_s = parse_timecode(result.get("timecode"))
        duration_s = self._extract_duration_s(result)
        parsed = RecognitionResult(
            track=TrackInfo(title=title, artist=artist, album=album),
            artwork_url=artwork_url,
            provider="audd",
            raw=result,
            cached=False,
            timecode_s=timecode_s,
            duration_s=duration_s,
            end_margin_s=self._end_margin_s,
            default_remaining_s=self._default_remaining_s,
        )
        logger.info(
            "AudD timing timecode=%ss duration=%ss remaining=%.1fs",
            f"{timecode_s:.1f}" if timecode_s is not None else "?",
            f"{duration_s:.1f}" if duration_s is not None else "?",
            parsed.remaining_s,
        )
        return parsed

    @staticmethod
    def _extract_duration_s(result: dict[str, Any]) -> float | None:
        apple = result.get("apple_music") or {}
        if apple.get("durationInMillis") is not None:
            try:
                return float(apple["durationInMillis"]) / 1000.0
            except (TypeError, ValueError):
                pass
        spotify = result.get("spotify") or {}
        if spotify.get("duration_ms") is not None:
            try:
                return float(spotify["duration_ms"]) / 1000.0
            except (TypeError, ValueError):
                pass
        if result.get("duration_ms") is not None:
            try:
                return float(result["duration_ms"]) / 1000.0
            except (TypeError, ValueError):
                pass
        return None

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
