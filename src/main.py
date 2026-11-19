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
from .config import describe_env, load_config
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
    p.add_argument(
        "--check",
        action="store_true",
        help="Print config/.env/token diagnostics and exit",
    )
    p.add_argument(
        "--list-samples",
        action="store_true",
        help="List captured sample WAV files (newest last)",
    )
    p.add_argument(
        "--play-sample",
        nargs="?",
        const="latest",
        default=None,
        help="Play a sample WAV (path or 'latest' / omit for newest)",
    )
    return p.parse_args()


def cmd_list_samples(config: dict) -> int:
    samples = AudioMonitor.list_samples(config)
    sample_dir = AudioMonitor.sample_dir_from_config(config)
    if not samples:
        print(f"No samples in {sample_dir}")
        return 1
    print(f"Samples in {sample_dir} ({len(samples)}):")
    print(f"{'#':>3}  {'DURATION':>8}  {'PEAK':>7}  {'RMS':>7}  {'BYTES':>8}  NAME")
    for i, path in enumerate(samples, start=1):
        try:
            q = AudioMonitor.wav_quality(path)
            print(
                f"{i:>3}  {float(q['duration_s']):>7.1f}s  {float(q['peak']):>7.4f}  "
                f"{float(q['rms']):>7.4f}  {int(q['bytes']):>8}  {path.name}"
            )
        except Exception as exc:
            print(f"{i:>3}  {'?':>8}  {'?':>7}  {'?':>7}  {path.stat().st_size:>8}  {path.name}  ({exc})")
    print("\nPlay newest:  auraframe --play-sample")
    print("Play file:    auraframe --play-sample /path/to/sample.wav")
    print("Play by # :   auraframe --play-sample 3")
    return 0


def cmd_play_sample(config: dict, spec: str) -> int:
    samples = AudioMonitor.list_samples(config)
    path: Path | None = None
    text = (spec or "latest").strip()

    if text in {"latest", "last", "newest", ""}:
        path = samples[-1] if samples else None
    elif text.isdigit():
        idx = int(text)
        if idx < 1 or idx > len(samples):
            logger.error("Sample #%s out of range (1-%s)", idx, len(samples))
            return 1
        path = samples[idx - 1]
    else:
        candidate = Path(text).expanduser()
        path = candidate if candidate.is_file() else None
        if path is None and samples:
            # allow bare filename match
            matches = [p for p in samples if p.name == text or text in p.name]
            path = matches[-1] if matches else None

    if path is None:
        logger.error("No sample to play (spec=%r). Run: auraframe --list-samples", text)
        return 1

    try:
        stats = AudioMonitor.play_wav(path)
    except Exception:
        logger.exception("Playback failed for %s", path)
        return 1

    print(
        f"Played {path}\n"
        f"  duration={float(stats['duration_s']):.1f}s  "
        f"peak={float(stats['peak']):.4f}  rms={float(stats['rms']):.4f}"
    )
    if float(stats["peak"]) < 0.05:
        print("  note: peak is very low — mic may be too quiet for AudD")
    elif float(stats["peak"]) > 0.99:
        print("  note: peak is clipped — lower gain or move mic back")
    return 0


def cmd_check(config: dict) -> int:
    root = Path(config["_root"])
    info = describe_env(root)
    print(f"root:            {info['root']}")
    print(f"config:          {config.get('_config_path')}")
    print(f"package_root:    {config.get('_package_root')}")
    print(f"env_files:       {config.get('_env_files')}")
    print(f"env_path:        {info['env_path']}")
    print(f"env_exists:      {info['env_exists']}")
    print(f"token_in_file:   {info['token_in_file']}")
    print(f"token_in_environ:{info['token_in_environ']}")
    print(f"token_mask:      {info['token_mask']}")
    recognizer = Recognizer(config)
    print(f"recognizer_ok:   {recognizer.configured}")
    if not recognizer.configured:
        print("\nFix: create .env next to config.yaml with:")
        print("  AUDD_API_TOKEN=your_token_here")
        return 1
    return 0


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
        self._done_miss = False
        self._last_key: str | None = None

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def submit(self, sample: AudioSample) -> bool:
        with self._lock:
            if self._busy:
                logger.info("Identify busy; deferring %s", Path(sample.path).name)
                return False
            self._busy = True
            self._done_miss = False
        logger.info("Identify thread start for %s", Path(sample.path).name)
        thread = threading.Thread(target=self._run, args=(sample,), daemon=True)
        thread.start()
        return True

    def take_result(self) -> RecognitionResult | None:
        with self._lock:
            result = self._result
            self._result = None
            return result

    def take_miss(self) -> bool:
        with self._lock:
            miss = self._done_miss
            self._done_miss = False
            return miss

    def _run(self, sample: AudioSample) -> None:
        result = None
        try:
            result = self._recognizer.identify(sample)
        except Exception:
            logger.exception("Background identify failed")
        finally:
            with self._lock:
                if result is not None:
                    self._result = result
                    self._last_key = result.track_key
                else:
                    self._done_miss = True
                self._busy = False
                logger.info(
                    "Identify thread done for %s result=%s",
                    Path(sample.path).name,
                    "match" if result else "none",
                )


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

    if args.check:
        return cmd_check(config)

    if args.list_samples:
        return cmd_list_samples(config)

    if args.play_sample is not None:
        return cmd_play_sample(config, args.play_sample)

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
            logger.error(
                "Live mode has no API token — AudD will NOT be called. "
                "Run: auraframe --check"
            )
        else:
            logger.info("Live recognition enabled")
        audio.start()
    display.show_idle()

    demo_shown = False
    demo_at = time.monotonic() + max(0.0, args.demo_delay)
    idle_timeout = float(config.get("display", {}).get("idle_timeout_s", 45))
    last_art_at: float | None = None
    last_track_key: str | None = None
    state = "idle"

    logger.info(
        "AuraFrame running live=%s root=%s",
        live,
        config.get("_root"),
    )

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
                    worker.submit(sample)
                elif sample and not recognizer.configured:
                    logger.error(
                        "Sample captured but token missing; skip AudD (%s)",
                        sample.path,
                    )
                    display.show_artwork(
                        Path(config["_assets"]) / "demo_art.png",
                        TrackInfo(
                            title="Music detected",
                            artist="no API token — run auraframe --check",
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
                elif worker.take_miss():
                    logger.info("Identify finished with no match")

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
