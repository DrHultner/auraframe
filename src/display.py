from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Any

import pygame
from PIL import Image

logger = logging.getLogger(__name__)


class DisplayState(Enum):
    IDLE = auto()
    ARTWORK = auto()


@dataclass(frozen=True)
class TrackInfo:
    title: str = ""
    artist: str = ""
    album: str = ""


@dataclass
class _Frame:
    surface: pygame.Surface
    track: TrackInfo | None = None
    state: DisplayState = DisplayState.IDLE


class DisplayEngine:
    """Fullscreen Pygame renderer for idle and album artwork."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._cfg = config.get("display", {})
        self._width = int(self._cfg.get("width", 1920))
        self._height = int(self._cfg.get("height", 1200))
        self._fullscreen = bool(self._cfg.get("fullscreen", True))
        self._overlay_enabled = bool(self._cfg.get("overlay", True))
        self._fade_ms = int(self._cfg.get("fade_ms", 400))
        self._fps = int(self._cfg.get("fps", 30))
        self._idle_color = tuple(self._cfg.get("idle_color", [12, 12, 18]))
        self._bg_color = tuple(self._cfg.get("background_color", [0, 0, 0]))
        self._overlay_bg_alpha = int(self._cfg.get("overlay_bg_alpha", 140))
        self._overlay_padding = int(self._cfg.get("overlay_padding", 24))
        self._assets = Path(config.get("_assets", "assets"))

        self._screen: pygame.Surface | None = None
        self._clock: pygame.time.Clock | None = None
        self._font: pygame.font.Font | None = None
        self._font_small: pygame.font.Font | None = None
        self._running = False
        self._lock = threading.Lock()
        self._current: _Frame | None = None
        self._pending: _Frame | None = None
        self._fade_start: float | None = None
        self._fade_from: pygame.Surface | None = None
        self._idle_logo: pygame.Surface | None = None

    def start(self) -> None:
        pygame.init()
        pygame.mouse.set_visible(False)
        flags = pygame.FULLSCREEN if self._fullscreen else 0
        self._screen = pygame.display.set_mode((self._width, self._height), flags)
        pygame.display.set_caption("AuraFrame")
        self._clock = pygame.time.Clock()
        self._font = pygame.font.SysFont("dejavusans", 42, bold=True)
        self._font_small = pygame.font.SysFont("dejavusans", 28)
        self._idle_logo = self._load_idle_logo()
        self._current = _Frame(surface=self._build_idle_surface())
        self._running = True
        logger.info(
            "Display started %sx%s fullscreen=%s",
            self._width,
            self._height,
            self._fullscreen,
        )

    def stop(self) -> None:
        self._running = False
        pygame.quit()
        logger.info("Display stopped")

    def show_idle(self) -> None:
        frame = _Frame(surface=self._build_idle_surface(), state=DisplayState.IDLE)
        self._queue_frame(frame)

    def show_artwork(self, image_path: str | Path, track: TrackInfo | None = None) -> None:
        path = Path(image_path)
        try:
            surface = self._load_artwork_surface(path)
        except Exception:
            logger.exception("Failed to load artwork: %s", path)
            return
        frame = _Frame(
            surface=surface,
            track=track or TrackInfo(),
            state=DisplayState.ARTWORK,
        )
        self._queue_frame(frame)
        logger.info(
            "Showing artwork title=%r artist=%r path=%s",
            (track.title if track else ""),
            (track.artist if track else ""),
            path,
        )

    def tick(self) -> bool:
        """Process events and redraw one frame. Returns False if quit requested."""
        if not self._running or self._screen is None or self._clock is None:
            return False

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return False
            if event.type == pygame.KEYDOWN and event.key in (pygame.K_ESCAPE, pygame.K_q):
                return False

        self._advance_fade()
        self._blit_current()
        pygame.display.flip()
        self._clock.tick(self._fps)
        return True

    def _queue_frame(self, frame: _Frame) -> None:
        with self._lock:
            if self._current is None:
                self._current = frame
                self._pending = None
                self._fade_start = None
                self._fade_from = None
                return
            self._fade_from = self._current.surface.copy()
            self._pending = frame
            self._fade_start = time.monotonic()

    def _advance_fade(self) -> None:
        with self._lock:
            if self._pending is None or self._fade_start is None:
                return
            elapsed_ms = (time.monotonic() - self._fade_start) * 1000.0
            if elapsed_ms >= self._fade_ms or self._fade_ms <= 0:
                self._current = self._pending
                self._pending = None
                self._fade_start = None
                self._fade_from = None

    def _blit_current(self) -> None:
        assert self._screen is not None
        with self._lock:
            current = self._current
            pending = self._pending
            fade_from = self._fade_from
            fade_start = self._fade_start

        if current is None:
            self._screen.fill(self._bg_color)
            return

        if pending is not None and fade_from is not None and fade_start is not None and self._fade_ms > 0:
            t = min(1.0, ((time.monotonic() - fade_start) * 1000.0) / self._fade_ms)
            self._screen.fill(self._bg_color)
            fade_from.set_alpha(int(255 * (1.0 - t)))
            pending.surface.set_alpha(int(255 * t))
            self._screen.blit(fade_from, (0, 0))
            self._screen.blit(pending.surface, (0, 0))
            pending.surface.set_alpha(255)
            fade_from.set_alpha(255)
            if pending.track and pending.state == DisplayState.ARTWORK:
                self._draw_overlay(pending.track, alpha_scale=t)
            return

        self._screen.blit(current.surface, (0, 0))
        if current.track and current.state == DisplayState.ARTWORK:
            self._draw_overlay(current.track)

    def _draw_overlay(self, track: TrackInfo, alpha_scale: float = 1.0) -> None:
        if not self._overlay_enabled or self._screen is None:
            return
        if not track.title and not track.artist:
            return
        assert self._font is not None and self._font_small is not None

        pad = self._overlay_padding
        lines: list[tuple[pygame.Surface, int]] = []
        if track.title:
            lines.append((self._font.render(track.title, True, (240, 240, 245)), 0))
        if track.artist:
            lines.append((self._font_small.render(track.artist, True, (190, 190, 200)), 8))

        width = max(s.get_width() for s, _ in lines) + pad * 2
        height = sum(s.get_height() + gap for s, gap in lines) + pad * 2 - (lines[-1][1] if lines else 0)
        bar = pygame.Surface((width, height), pygame.SRCALPHA)
        alpha = int(self._overlay_bg_alpha * alpha_scale)
        bar.fill((0, 0, 0, alpha))

        y = pad
        for surf, gap in lines:
            surf = surf.copy()
            surf.set_alpha(int(255 * alpha_scale))
            bar.blit(surf, (pad, y))
            y += surf.get_height() + gap

        x = (self._width - width) // 2
        y = self._height - height - 48
        self._screen.blit(bar, (x, y))

    def _build_idle_surface(self) -> pygame.Surface:
        surface = pygame.Surface((self._width, self._height))
        surface.fill(self._idle_color)
        if self._idle_logo is not None:
            rect = self._idle_logo.get_rect(center=(self._width // 2, self._height // 2))
            surface.blit(self._idle_logo, rect)
        elif self._font is not None:
            label = self._font.render("AuraFrame", True, (80, 80, 100))
            rect = label.get_rect(center=(self._width // 2, self._height // 2))
            surface.blit(label, rect)
        return surface

    def _load_idle_logo(self) -> pygame.Surface | None:
        for name in ("idle.png", "logo.png", "idle.jpg"):
            path = self._assets / name
            if path.is_file():
                try:
                    return self._scale_to_fit(self._pil_to_surface(Image.open(path)), max_ratio=0.35)
                except Exception:
                    logger.exception("Failed loading idle asset %s", path)
        return None

    def _load_artwork_surface(self, path: Path) -> pygame.Surface:
        image = Image.open(path).convert("RGB")
        art = self._pil_to_surface(image)
        fitted = self._scale_to_fit(art, max_ratio=1.0)
        canvas = pygame.Surface((self._width, self._height))
        canvas.fill(self._bg_color)
        rect = fitted.get_rect(center=(self._width // 2, self._height // 2))
        canvas.blit(fitted, rect)
        return canvas

    def _scale_to_fit(self, surface: pygame.Surface, max_ratio: float = 1.0) -> pygame.Surface:
        max_w = int(self._width * max_ratio)
        max_h = int(self._height * max_ratio)
        sw, sh = surface.get_size()
        if sw == 0 or sh == 0:
            return surface
        scale = min(max_w / sw, max_h / sh)
        new_size = (max(1, int(sw * scale)), max(1, int(sh * scale)))
        return pygame.transform.smoothscale(surface, new_size)

    @staticmethod
    def _pil_to_surface(image: Image.Image) -> pygame.Surface:
        if image.mode != "RGB":
            image = image.convert("RGB")
        data = image.tobytes()
        return pygame.image.fromstring(data, image.size, "RGB")
