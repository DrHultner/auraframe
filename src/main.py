from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
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
    return p.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    setup_logging(config.get("logging", {}).get("level", "INFO"))

    if args.windowed:
        config.setdefault("display", {})["fullscreen"] = False

    display = DisplayEngine(config)
    artwork = ArtworkManager(config)
    audio = AudioMonitor(config)
    recognizer = Recognizer(config)

    stopping = False

    def _handle_signal(signum: int, _frame: object) -> None:
        nonlocal stopping
        logger.info("Signal %s received, shutting down", signum)
        stopping = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    display.start()
    audio.start()
    display.show_idle()

    demo_shown = False
    demo_at = time.monotonic() + max(0.0, args.demo_delay)
    idle_timeout = float(config.get("display", {}).get("idle_timeout_s", 45))
    last_art_at: float | None = None
    state = "idle"

    logger.info("AuraFrame running (phase 2: display + artwork cache)")

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

            # Later phases: sample = audio.poll(); recognizer.identify(sample)
            _ = recognizer  # reserved for phase 4+

            if state == "artwork" and last_art_at is not None:
                if time.monotonic() - last_art_at >= idle_timeout:
                    display.show_idle()
                    state = "idle"
                    last_art_at = None

    finally:
        audio.stop()
        display.stop()

    logger.info("AuraFrame exited cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
