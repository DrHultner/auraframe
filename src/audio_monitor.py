from __future__ import annotations

import logging
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


def _sounddevice():
    try:
        import sounddevice as sd
    except OSError as exc:
        raise RuntimeError(
            "PortAudio not available. On Debian/Ubuntu/Pi OS install: "
            "sudo apt-get install -y portaudio19-dev libportaudio2"
        ) from exc
    return sd


@dataclass
class AudioSample:
    path: str
    duration_s: float
    rms: float = 0.0
    band_ratio: float = 0.0


@dataclass(frozen=True)
class GateReading:
    rms: float
    band_ratio: float
    music_likely: bool


class AudioMonitor:
    """Continuous mic capture with a lightweight music detection gate."""

    def __init__(self, config: dict[str, Any]) -> None:
        cfg = config.get("audio", {})
        self._device = cfg.get("device", None)
        self._sample_rate = int(cfg.get("sample_rate", 44100))
        self._channels = int(cfg.get("channels", 1))
        self._sample_seconds = float(cfg.get("sample_seconds", 10))
        self._block_size = int(cfg.get("block_size", 2048))
        self._gate_rms = float(cfg.get("gate_rms_threshold", 0.02))
        self._gate_band_ratio = float(cfg.get("gate_band_ratio_min", 0.25))
        self._gate_min_hz = float(cfg.get("gate_min_hz", 200))
        self._gate_max_hz = float(cfg.get("gate_max_hz", 5000))
        self._gate_window_s = float(cfg.get("gate_window_s", 1.5))
        self._cooldown_s = float(cfg.get("cooldown_s", 30))
        self._check_interval_s = float(cfg.get("check_interval_s", 1.0))
        self._silence_interval_s = float(cfg.get("silence_interval_s", 2.5))
        self._gate_hits_required = int(cfg.get("gate_hits_required", 3))
        self._normalize = bool(cfg.get("normalize", True))
        self._target_peak = float(cfg.get("target_peak", 0.9))
        self._max_gain = float(cfg.get("max_gain", 40.0))
        self._dtype = "float32"

        sample_dir = cfg.get("sample_dir", "~/.cache/auraframe/samples")
        self._sample_dir = Path(sample_dir).expanduser()
        self._max_sample_files = int(cfg.get("max_sample_files", 20))

        self._buffer_seconds = self._sample_seconds + self._gate_window_s + 1.0
        self._max_samples = int(self._buffer_seconds * self._sample_rate)
        self._buffer = deque(maxlen=self._max_samples)
        self._lock = threading.Lock()
        self._stream = None
        self._running = False
        self._last_check_at = 0.0
        self._last_sample_at = 0.0
        self._last_music_at = 0.0
        self._music_hits = 0
        self._last_reading = GateReading(0.0, 0.0, False)
        self._frames_seen = 0

    @property
    def last_reading(self) -> GateReading:
        return self._last_reading

    def start(self) -> None:
        if self._running:
            return
        self._sample_dir.mkdir(parents=True, exist_ok=True)
        sd = _sounddevice()
        device = self._resolve_device(self._device)
        try:
            self._stream = sd.InputStream(
                samplerate=self._sample_rate,
                channels=self._channels,
                dtype=self._dtype,
                blocksize=self._block_size,
                device=device,
                callback=self._on_audio,
            )
            self._stream.start()
        except Exception:
            logger.exception(
                "Failed to open audio input device=%r rate=%s",
                device,
                self._sample_rate,
            )
            raise

        self._running = True
        logger.info(
            "AudioMonitor started device=%r rate=%s ch=%s gate_rms=%.4f band_min=%.2f",
            device,
            self._sample_rate,
            self._channels,
            self._gate_rms,
            self._gate_band_ratio,
        )

    def stop(self) -> None:
        self._running = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                logger.debug("Error closing audio stream", exc_info=True)
            self._stream = None
        logger.info("AudioMonitor stopped")

    def poll(self) -> AudioSample | None:
        """Return a new WAV sample when music is likely and cooldown allows."""
        if not self._running:
            return None

        now = time.monotonic()
        interval = self._check_interval_s
        if not self._last_reading.music_likely:
            interval = self._silence_interval_s
        if now - self._last_check_at < interval:
            return None
        self._last_check_at = now

        reading = self._analyze_gate()
        self._last_reading = reading
        if reading.music_likely:
            self._last_music_at = now
            self._music_hits += 1
            logger.info(
                "Music likely hit=%s/%s rms=%.4f band_ratio=%.3f",
                self._music_hits,
                self._gate_hits_required,
                reading.rms,
                reading.band_ratio,
            )
        else:
            self._music_hits = 0
            logger.debug(
                "No music rms=%.4f band_ratio=%.3f",
                reading.rms,
                reading.band_ratio,
            )
            return None

        if self._music_hits < self._gate_hits_required:
            return None

        if now - self._last_sample_at < self._cooldown_s:
            logger.debug(
                "Music gate open but cooldown active (%.1fs left)",
                self._cooldown_s - (now - self._last_sample_at),
            )
            return None

        sample = self._extract_sample(reading)
        if sample is not None:
            self._last_sample_at = now
            self._cleanup_old_samples()
            logger.info(
                "Captured sample path=%s duration=%.1fs rms=%.4f",
                sample.path,
                sample.duration_s,
                sample.rms,
            )
        return sample

    def peek_level(self) -> GateReading:
        """Force a gate analysis (for monitoring UIs / CLI)."""
        reading = self._analyze_gate()
        self._last_reading = reading
        return reading

    @staticmethod
    def list_devices() -> list[dict[str, Any]]:
        sd = _sounddevice()
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        rows: list[dict[str, Any]] = []
        for idx, dev in enumerate(devices):
            if int(dev.get("max_input_channels", 0)) <= 0:
                continue
            api = hostapis[int(dev["hostapi"])]["name"]
            rows.append(
                {
                    "index": idx,
                    "name": dev["name"],
                    "channels": int(dev["max_input_channels"]),
                    "default_rate": float(dev.get("default_samplerate", 0)),
                    "hostapi": api,
                }
            )
        return rows

    @staticmethod
    def sample_dir_from_config(config: dict[str, Any]) -> Path:
        cfg = config.get("audio", {})
        return Path(cfg.get("sample_dir", "~/.cache/auraframe/samples")).expanduser()

    @classmethod
    def list_samples(cls, config: dict[str, Any]) -> list[Path]:
        sample_dir = cls.sample_dir_from_config(config)
        if not sample_dir.is_dir():
            return []
        return sorted(sample_dir.glob("sample-*.wav"), key=lambda p: p.stat().st_mtime)

    @staticmethod
    def read_wav(path: Path) -> tuple[np.ndarray, int]:
        with wave.open(str(path), "rb") as wf:
            rate = int(wf.getframerate())
            ch = int(wf.getnchannels())
            width = int(wf.getsampwidth())
            frames = wf.readframes(wf.getnframes())
        if width != 2:
            raise ValueError(f"Unsupported sample width {width} (want 16-bit)")
        data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        if ch > 1:
            data = data.reshape(-1, ch)
        return data, rate

    @classmethod
    def wav_quality(cls, path: Path) -> dict[str, float | int | str]:
        data, rate = cls.read_wav(path)
        mono = data.mean(axis=1) if data.ndim > 1 else data
        peak = float(np.max(np.abs(mono))) if mono.size else 0.0
        rms = float(np.sqrt(np.mean(np.square(mono)))) if mono.size else 0.0
        duration = float(mono.size / max(rate, 1))
        return {
            "path": str(path),
            "bytes": path.stat().st_size,
            "rate": rate,
            "duration_s": duration,
            "peak": peak,
            "rms": rms,
        }

    @classmethod
    def play_wav(cls, path: Path, *, blocking: bool = True) -> dict[str, float | int | str]:
        """Play a WAV on the default output device; return quality stats."""
        path = Path(path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(path)
        data, rate = cls.read_wav(path)
        stats = cls.wav_quality(path)
        sd = _sounddevice()
        logger.info(
            "Playing %s duration=%.1fs peak=%.4f rms=%.4f rate=%s",
            path.name,
            float(stats["duration_s"]),
            float(stats["peak"]),
            float(stats["rms"]),
            rate,
        )
        sd.play(data, samplerate=rate)
        if blocking:
            sd.wait()
        return stats

    def _on_audio(self, indata: np.ndarray, frames: int, _time: Any, status: Any) -> None:
        if status:
            logger.warning("Audio status: %s", status)
        mono = indata.copy()
        if mono.ndim > 1:
            mono = mono.mean(axis=1)
        else:
            mono = mono.reshape(-1)
        with self._lock:
            self._buffer.extend(mono.astype(np.float32, copy=False))
            self._frames_seen += frames

    def _analyze_gate(self) -> GateReading:
        window = self._get_recent(self._gate_window_s)
        if window.size < max(256, int(0.25 * self._sample_rate)):
            return GateReading(0.0, 0.0, False)

        rms = float(np.sqrt(np.mean(np.square(window))))
        band_ratio = self._band_energy_ratio(window)
        music = rms >= self._gate_rms and band_ratio >= self._gate_band_ratio
        return GateReading(rms=rms, band_ratio=band_ratio, music_likely=music)

    def _band_energy_ratio(self, samples: np.ndarray) -> float:
        # Hann window + rFFT; ratio of energy in music-ish band vs total
        n = samples.size
        windowed = samples * np.hanning(n)
        spectrum = np.fft.rfft(windowed)
        power = np.abs(spectrum) ** 2
        freqs = np.fft.rfftfreq(n, d=1.0 / self._sample_rate)
        total = float(np.sum(power)) + 1e-12
        band = (freqs >= self._gate_min_hz) & (freqs <= self._gate_max_hz)
        return float(np.sum(power[band]) / total)

    def _extract_sample(self, reading: GateReading) -> AudioSample | None:
        samples = self._get_recent(self._sample_seconds)
        need = int(self._sample_seconds * self._sample_rate * 0.9)
        if samples.size < need:
            logger.warning(
                "Not enough audio buffered yet (%s < %s)",
                samples.size,
                need,
            )
            return None

        peak_in = float(np.max(np.abs(samples))) if samples.size else 0.0
        rms_in = float(np.sqrt(np.mean(np.square(samples)))) if samples.size else 0.0
        samples, gain = self._normalize_audio(samples)
        peak_out = float(np.max(np.abs(samples))) if samples.size else 0.0
        rms_out = float(np.sqrt(np.mean(np.square(samples)))) if samples.size else 0.0

        ts = time.strftime("%Y%m%d-%H%M%S")
        path = self._sample_dir / f"sample-{ts}.wav"
        self._write_wav(path, samples)
        logger.info(
            "Sample audio in_peak=%.4f in_rms=%.4f gain=%.1fx out_peak=%.4f out_rms=%.4f",
            peak_in,
            rms_in,
            gain,
            peak_out,
            rms_out,
        )
        return AudioSample(
            path=str(path),
            duration_s=samples.size / float(self._sample_rate),
            rms=rms_out if rms_out > 0 else reading.rms,
            band_ratio=reading.band_ratio,
        )

    def _normalize_audio(self, samples: np.ndarray) -> tuple[np.ndarray, float]:
        if not self._normalize or samples.size == 0:
            return samples, 1.0
        peak = float(np.max(np.abs(samples)))
        if peak < 1e-6:
            return samples, 1.0
        gain = min(self._target_peak / peak, self._max_gain)
        if gain < 1.01:
            return samples, 1.0
        return (samples * gain).astype(np.float32, copy=False), float(gain)

    def _get_recent(self, seconds: float) -> np.ndarray:
        n = int(seconds * self._sample_rate)
        with self._lock:
            if not self._buffer:
                return np.zeros(0, dtype=np.float32)
            data = np.fromiter(self._buffer, dtype=np.float32, count=len(self._buffer))
        if data.size <= n:
            return data
        return data[-n:]

    def _write_wav(self, path: Path, samples: np.ndarray) -> None:
        clipped = np.clip(samples, -1.0, 1.0)
        pcm = (clipped * 32767.0).astype(np.int16)
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self._sample_rate)
            wf.writeframes(pcm.tobytes())

    def _cleanup_old_samples(self) -> None:
        files = sorted(self._sample_dir.glob("sample-*.wav"), key=lambda p: p.stat().st_mtime)
        excess = len(files) - self._max_sample_files
        for path in files[: max(0, excess)]:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.debug("Could not remove sample %s", path, exc_info=True)

    @staticmethod
    def _resolve_device(device: Any) -> Any:
        if device is None or device == "" or device == "null":
            return None
        if isinstance(device, int):
            return device
        text = str(device).strip()
        if text.isdigit():
            return int(text)
        # Match by substring name
        matches = []
        for row in AudioMonitor.list_devices():
            if text.lower() in str(row["name"]).lower():
                matches.append(row["index"])
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            logger.warning("Multiple devices match %r indices=%s; using %s", text, matches, matches[0])
            return matches[0]
        logger.warning("No input device matched %r; using default", text)
        return None
