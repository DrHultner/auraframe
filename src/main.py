from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .artwork import ArtworkManager
from .audio_monitor import AudioMonitor, AudioSample
from .config import load_config
from .display import DisplayEngine, TrackInfo
from .recognizer import RecognitionResult, Recognizer

logger = logging.getLogger(__name__)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AuraFrame ambient music display")
    p.add_argument("--config", type=Path, default=None, help="Path to config.yaml")
    p.add_argument(
        "--windowed",
        action="store_true",
        help="Run display in a window (dev mode)",
    )
    p.add_argument(
        "--demo-art",
        default=None,
        help="Local path or URL to show as artwork (demo)",
    )
    p.add_argument("--title", default="Demo Track", help="Overlay title for demo art")
    p.add_argument("--artist", default="Demo Artist", help="Overlay artist for demo art")
    p.add_argument(
        "--demo-delay",
        type=float,
        default=2.0,
        help="Seconds before showing demo art",
    )
    p.add_argument(
        "--list-devices",
        action="store_true",
        help="List audio input devices and exit",
    )
    p.add_argument(
        "--monitor-only",
        action="store_true",
        help="Run audio gate only (no display); log samples when music is likely",
    )
    p.add_argument(
        "--audio-demo",
        action="store_true",
        help="Enable live audio gate + recognition in the main UI loop",
    )
    p.add_argument(
        "--live",
        action="store_true",
        help="Full appliance mode: audio + recognize + artwork display",
    )
    p.add_argument(
        "--recognize-file",
        type=Path,
        default=None,
        help="Identify a local WAV/MP3 sample and print the result",
    )
    p.add_argument(
        "--show-result",
        action="store_true",
        help="With --recognize-file, also show artwork on the display",
    )
    return p.parse_args()


def cmd_list_devices() -> int:
    rows = AudioMonitor.list_devices()
    if not rows:
        print("No input devices found.")
        return 1
    print(f"{'IDX':>4}  {'CH':>3}  {'RATE':>8}  NAME")
    for row in rows:
        print(
            f"{row['index']:>4}  {row['channels']:>3}  {row['default_rate']:>8.0f}  "
            f"{row['name']} ({row['hostapi']})"
        )
    return 0


def cmd_recognize_file(config: dict, path: Path, show: bool, windowed: bool) -> int:
    if not path.is_file():
        logger.error("File not found: %s", path)
        return 1

    recognizer = Recognizer(config)
    if not recognizer.configured:
        logger.error("Set AUDD_API_TOKEN in .env first")
        return 1

    sample = AudioSample(path=str(path), duration_s=0.0)
    result = recognizer.identify(sample)
    if result is None:
        print("No match")
        return 2

    print(f"{result.track.artist} — {result.track.title}")
    if result.track.album:
        print(f"Album: {result.track.album}")
    if result.artwork_url:
        print(f"Art:   {result.artwork_url}")
    print(f"Cached: {result.cached}")

    if not show:
        return 0

    if windowed:
        config.setdefault("display", {})["fullscreen"] = False
    artwork = ArtworkManager(config)
    display = DisplayEngine(config)
    display.start()
    try:
        _apply_result(display, artwork, result)
        idle_timeout = float(config.get("display", {}).get("idle_timeout_s", 45))
        end_at = time.monotonic() + idle_timeout
        while time.monotonic() < end_at:
            if not display.tick():
                break
    finally:
        display.stop()
    return 0


def run_monitor_only(config: dict, stopping: Callable[[], bool]) -> int:
    audio = AudioMonitor(config)
    recognizer = Recognizer(config)
    if not recognizer.configured:
        logger.error(
            "AUDD_API_TOKEN not loaded. Put it in %s/.env or ./.env then restart.",
            config.get("_root"),
        )
        return 1

    audio.start()
    logger.info("Monitor-only mode + recognize; Ctrl+C to stop")
    try:
        while not stopping():
            sample = audio.poll()
            if sample:
                logger.info(
                    "SAMPLE ready %s (%.1fs) rms=%.4f band=%.3f",
                    sample.path,
                    sample.duration_s,
                    sample.rms,
                    sample.band_ratio,
                )
                result = recognizer.identify(sample)
                if result:
                    logger.info(
                        "MATCH %s — %s art=%s cached=%s",
                        result.track.artist,
                        result.track.title,
                        result.artwork_url or "-",
                        result.cached,
                    )
                else:
                    logger.info("No AudD match for %s", Path(sample.path).name)
            else:
                reading = audio.last_reading
                logger.info(
                    "level rms=%.4f band=%.3f music=%s",
                    reading.rms,
                    reading.band_ratio,
                    reading.music_likely,
                )
            time.sleep(0.25)
    finally:
        audio.stop()
    return 0


class _IdentifyWorker:
    """Run recognition off the UI thread."""

    def __init__(self, recognizer: Recognizer) -> None:
        self._recognizer = recognizer
        self._lock = threading.Lock()
        self._busy = False
        self._result: RecognitionResult | None = None
        self._last_key: str | None = None

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def submit(self, sample: AudioSample) -> bool:
        with self._lock:
            if self._busy:
                return False
            self._busy = True
        thread = threading.Thread(target=self._run, args=(sample,), daemon=True)
        thread.start()
        return True

    def take_result(self) -> RecognitionResult | None:
        with self._lock:
            result = self._result
            self._result = None
            return result

    def _run(self, sample: AudioSample) -> None:
        result = None
        try:
            result = self._recognizer.identify(sample)
        except Exception:
            logger.exception("Background identify failed")
        finally:
            with self._lock:
                if result is not None:
                    # Still deliver duplicates so caller can refresh hold timer;
                    # orchestrator decides whether to re-render.
                    self._result = result
                    self._last_key = result.track_key
                self._busy = False


def _apply_result(
    display: DisplayEngine,
    artwork: ArtworkManager,
    result: RecognitionResult,
) -> bool:
    path = None
    if result.artwork_url:
        path = artwork.ensure(result.artwork_url, result.track)
    if path is None:
        # Fallback placeholder so metadata still shows
        assets = Path(artwork.cache_dir)
        logger.warning("No artwork available; using idle/demo fallback")
        demo = Path(__file__).resolve().parent.parent / "assets" / "demo_art.png"
        path = demo if demo.is_file() else None
    if path is None:
        return False
    display.show_artwork(path, result.track)
    return True


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    setup_logging(config.get("logging", {}).get("level", "INFO"))

    if args.list_devices:
        return cmd_list_devices()

    if args.recognize_file is not None:
        return cmd_recognize_file(
            config,
            args.recognize_file,
            show=args.show_result,
            windowed=args.windowed,
        )

    stopping = False

    def _handle_signal(signum: int, _frame: object) -> None:
        nonlocal stopping
        logger.info("Signal %s received, shutting down", signum)
        stopping = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    live = bool(args.live or args.audio_demo)

    if args.monitor_only:
        return run_monitor_only(config, lambda: stopping)

    if args.windowed:
        config.setdefault("display", {})["fullscreen"] = False

    display = DisplayEngine(config)
    artwork = ArtworkManager(config)
    audio = AudioMonitor(config)
    recognizer = Recognizer(config)
    worker = _IdentifyWorker(recognizer) if live else None

    display.start()
    if live:
        if not recognizer.configured:
            logger.warning("Live mode without API token — gate only, no identify")
        audio.start()
    display.show_idle()

    demo_shown = False
    demo_at = time.monotonic() + max(0.0, args.demo_delay)
    idle_timeout = float(config.get("display", {}).get("idle_timeout_s", 45))
    last_art_at: float | None = None
    last_track_key: str | None = None
    state = "idle"

    logger.info("AuraFrame running (phase 4: recognition%s)", " live" if live else "")

    try:
        while not stopping:
            if not display.tick():
                break

            if args.demo_art and not demo_shown and time.monotonic() >= demo_at:
                path = artwork.ensure(args.demo_art)
                if path:
                    display.show_artwork(
                        path,
                        TrackInfo(title=args.title, artist=args.artist),
                    )
                    last_art_at = time.monotonic()
                    state = "artwork"
                    demo_shown = True
                else:
                    logger.error("Demo art unavailable: %s", args.demo_art)
                    demo_shown = True

            if live and worker is not None:
                sample = audio.poll()
                if sample and recognizer.configured:
                    if worker.submit(sample):
                        logger.info("Identify queued for %s", Path(sample.path).name)
                elif sample and not recognizer.configured:
                    display.show_artwork(
                        Path(config["_assets"]) / "demo_art.png",
                        TrackInfo(
                            title="Music detected",
                            artist=f"rms={sample.rms:.3f} (no API token)",
                        ),
                    )
                    last_art_at = time.monotonic()
                    state = "artwork"

                result = worker.take_result()
                if result is not None:
                    same = last_track_key == result.track_key
                    last_track_key = result.track_key
                    last_art_at = time.monotonic()
                    state = "artwork"
                    if same:
                        logger.info("Same track still playing: %s", result.track_key)
                    else:
                        _apply_result(display, artwork, result)

            if state == "artwork" and last_art_at is not None:
                if time.monotonic() - last_art_at >= idle_timeout:
                    display.show_idle()
                    state = "idle"
                    last_art_at = None
                    last_track_key = None

    finally:
        if live:
            audio.stop()
        display.stop()

    logger.info("AuraFrame exited cleanly")
    return 0


def cli() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    cli()
