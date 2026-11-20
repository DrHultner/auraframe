from __future__ import annotations

import csv
import logging
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

YAMNET_SR = 16000
YAMNET_PATCH = 15600  # fixed input length for TF-Hub classification tflite
YAMNET_MODEL_URL = (
    "https://tfhub.dev/google/lite-model/yamnet/classification/tflite/1?lite-format=tflite"
)
YAMNET_CLASSMAP_URL = (
    "https://raw.githubusercontent.com/tensorflow/models/master/"
    "research/audioset/yamnet/yamnet_class_map.csv"
)

# Prefer these labels when scoring "music-ness"
_DEFAULT_MUSIC_LABELS = {
    "Music",
    "Singing",
    "Song",
    "Musical instrument",
    "Pop music",
    "Rock music",
    "Hip hop music",
    "Electronic music",
    "Classical music",
    "Jazz",
    "Ambient music",
    "Dance music",
    "Background music",
    "Vocal music",
    "Independent music",
}


@dataclass(frozen=True)
class MusicDecision:
    is_music: bool
    score: float
    threshold: float
    top_labels: tuple[tuple[str, float], ...] = ()


def _load_interpreter(model_path: Path):
    try:
        from ai_edge_litert.interpreter import Interpreter  # type: ignore
        return Interpreter(model_path=str(model_path))
    except Exception:
        pass
    try:
        from tflite_runtime.interpreter import Interpreter  # type: ignore
        return Interpreter(model_path=str(model_path))
    except Exception:
        pass
    try:
        from tensorflow.lite.python.interpreter import Interpreter
        return Interpreter(model_path=str(model_path))
    except Exception as exc:
        raise RuntimeError(
            "No TFLite interpreter found. Install one of: "
            "ai-edge-litert, tflite-runtime, or tensorflow"
        ) from exc


def resample_mono(samples: np.ndarray, orig_sr: int, target_sr: int = YAMNET_SR) -> np.ndarray:
    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    if orig_sr == target_sr or x.size == 0:
        return x
    duration = x.size / float(orig_sr)
    n = max(1, int(round(duration * target_sr)))
    # linear resample — fine for classifier input
    t_old = np.linspace(0.0, 1.0, x.size, endpoint=False)
    t_new = np.linspace(0.0, 1.0, n, endpoint=False)
    return np.interp(t_new, t_old, x).astype(np.float32)


class YamnetGate:
    """YAMNet TFLite music-content gate over a fixed audio window."""

    def __init__(self, config: dict[str, Any]) -> None:
        cfg = config.get("yamnet", {})
        root = Path(config.get("_root") or ".")
        default_model = root / "assets" / "models" / "yamnet.tflite"
        default_map = root / "assets" / "models" / "yamnet_class_map.csv"
        self._model_path = Path(cfg.get("model_path") or default_model).expanduser()
        self._class_map_path = Path(cfg.get("class_map_path") or default_map).expanduser()
        self._threshold = float(cfg.get("music_threshold", 0.35))
        self._hop_s = float(cfg.get("hop_s", 0.48))
        labels = cfg.get("music_labels")
        self._music_label_names = set(labels) if labels else set(_DEFAULT_MUSIC_LABELS)
        self._ensure_assets()
        self._class_names = self._load_class_names(self._class_map_path)
        self._music_indices = [
            i for i, name in enumerate(self._class_names) if name in self._music_label_names
        ]
        if not self._music_indices:
            # fallback: class 132 is Music in AudioSet/YAMNet
            self._music_indices = [132]

        self._interpreter = _load_interpreter(self._model_path)
        self._interpreter.allocate_tensors()
        self._input = self._interpreter.get_input_details()[0]
        self._output = self._interpreter.get_output_details()[0]
        logger.info(
            "YAMNet ready model=%s threshold=%.2f music_classes=%s",
            self._model_path.name,
            self._threshold,
            len(self._music_indices),
        )

    def evaluate_wav(self, path: str | Path, sample_rate: int | None = None) -> MusicDecision:
        import wave

        path = Path(path)
        with wave.open(str(path), "rb") as wf:
            rate = int(wf.getframerate())
            ch = int(wf.getnchannels())
            width = int(wf.getsampwidth())
            frames = wf.readframes(wf.getnframes())
        if width != 2:
            raise ValueError("YAMNet gate expects 16-bit WAV")
        data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        if ch > 1:
            data = data.reshape(-1, ch).mean(axis=1)
        if sample_rate is not None:
            rate = sample_rate
        return self.evaluate_samples(data, rate)

    def evaluate_samples(self, samples: np.ndarray, sample_rate: int) -> MusicDecision:
        audio = resample_mono(samples, sample_rate, YAMNET_SR)
        if audio.size < YAMNET_PATCH:
            # pad short clips
            padded = np.zeros(YAMNET_PATCH, dtype=np.float32)
            padded[: audio.size] = audio
            patches = [padded]
        else:
            hop = max(1, int(self._hop_s * YAMNET_SR))
            patches = []
            start = 0
            while start + YAMNET_PATCH <= audio.size:
                patches.append(audio[start : start + YAMNET_PATCH])
                start += hop
            if not patches:
                patches = [audio[:YAMNET_PATCH]]
            # ensure tail coverage
            tail = audio[-YAMNET_PATCH:]
            if len(patches) == 0 or not np.allclose(patches[-1], tail):
                patches.append(tail)

        frame_scores: list[np.ndarray] = []
        for patch in patches:
            scores = self._infer_patch(patch)
            frame_scores.append(scores)

        stacked = np.stack(frame_scores, axis=0)  # [P, 521]
        music_per_patch = stacked[:, self._music_indices].max(axis=1)
        score = float(np.mean(music_per_patch))

        mean_scores = stacked.mean(axis=0)
        top_idx = np.argsort(mean_scores)[::-1][:5]
        top_labels = tuple(
            (self._class_names[i] if i < len(self._class_names) else str(i), float(mean_scores[i]))
            for i in top_idx
        )
        is_music = score >= self._threshold
        logger.info(
            "YAMNet music_score=%.3f threshold=%.2f is_music=%s top=%s",
            score,
            self._threshold,
            is_music,
            ", ".join(f"{n}={v:.2f}" for n, v in top_labels),
        )
        return MusicDecision(
            is_music=is_music,
            score=score,
            threshold=self._threshold,
            top_labels=top_labels,
        )

    def _infer_patch(self, patch: np.ndarray) -> np.ndarray:
        x = np.asarray(patch, dtype=np.float32).reshape(-1)
        if x.size < YAMNET_PATCH:
            pad = np.zeros(YAMNET_PATCH, dtype=np.float32)
            pad[: x.size] = x
            x = pad
        elif x.size > YAMNET_PATCH:
            x = x[:YAMNET_PATCH]
        self._interpreter.set_tensor(self._input["index"], x)
        self._interpreter.invoke()
        out = self._interpreter.get_tensor(self._output["index"])
        return np.asarray(out, dtype=np.float32).reshape(-1)

    def _ensure_assets(self) -> None:
        self._model_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._model_path.is_file() or self._model_path.stat().st_size < 1000:
            logger.info("Downloading YAMNet model → %s", self._model_path)
            urllib.request.urlretrieve(YAMNET_MODEL_URL, self._model_path)
        if not self._class_map_path.is_file():
            logger.info("Downloading YAMNet class map → %s", self._class_map_path)
            urllib.request.urlretrieve(YAMNET_CLASSMAP_URL, self._class_map_path)

    @staticmethod
    def _load_class_names(path: Path) -> list[str]:
        names: list[str] = []
        with path.open(encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                names.append(str(row.get("display_name") or row.get("name") or ""))
        return names
