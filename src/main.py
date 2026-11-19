from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path

from .artwork import ArtworkManager
from .audio_monitor import AudioMonitor
from .config import load_config
from .display import DisplayEngine, TrackInfo
from .recognizer import Recognizer

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
        help="Enable live audio gate in the main loop (phase 3)",
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


def run_monitor_only(config: dict, stopping: Callable[[], bool]) -> int:
    audio = AudioMonitor(config)
    audio.start()
    logger.info("Monitor-only mode; Ctrl+C to stop")
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


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    setup_logging(config.get("logging", {}).get("level", "INFO"))

    if args.list_devices:
        return cmd_list_devices()

    stopping = False

    def _handle_signal(signum: int, _frame: object) -> None:
        nonlocal stopping
        logger.info("Signal %s received, shutting down", signum)
        stopping = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    if args.monitor_only:
        return run_monitor_only(config, lambda: stopping)

    if args.windowed:
        config.setdefault("display", {})["fullscreen"] = False

    display = DisplayEngine(config)
    artwork = ArtworkManager(config)
    audio = AudioMonitor(config)
    recognizer = Recognizer(config)

    display.start()
    if args.audio_demo:
        audio.start()
    display.show_idle()

    demo_shown = False
    demo_at = time.monotonic() + max(0.0, args.demo_delay)
    idle_timeout = float(config.get("display", {}).get("idle_timeout_s", 45))
    last_art_at: float | None = None
    state = "idle"
    samples_captured = 0

    logger.info("AuraFrame running (phase 3: audio monitor + gate)")

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

            if args.audio_demo:
                sample = audio.poll()
                if sample:
                    samples_captured += 1
                    # Phase 3: no recognition yet — flash overlay as proof of gate
                    display.show_artwork(
                        Path(config["_assets"]) / "demo_art.png",
                        TrackInfo(
                            title=f"Music detected #{samples_captured}",
                            artist=f"rms={sample.rms:.3f}  {Path(sample.path).name}",
                        ),
                    )
                    last_art_at = time.monotonic()
                    state = "artwork"

            _ = recognizer  # reserved for phase 4+

            if state == "artwork" and last_art_at is not None:
                if time.monotonic() - last_art_at >= idle_timeout:
                    display.show_idle()
                    state = "idle"
                    last_art_at = None

    finally:
        if args.audio_demo:
            audio.stop()
        display.stop()

    logger.info("AuraFrame exited cleanly")
    return 0


def cli() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    cli()
