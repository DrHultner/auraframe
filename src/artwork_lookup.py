from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus

import requests

from .display import TrackInfo

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArtworkHit:
    url: str
    source: str
    score: float
    width: int = 0


def _norm(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", text)  # drop (feat. …) etc.
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _token_set(text: str) -> set[str]:
    return {t for t in _norm(text).split() if t and t not in {"the", "a", "an", "and", "feat", "ft", "with"}}


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    na, nb = _norm(a), _norm(b)
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.9
    ta, tb = _token_set(a), _token_set(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


class ArtworkLookup:
    """Resolve album cover URLs from iTunes, MusicBrainz/CAA, and optional Spotify."""

    def __init__(self, config: dict[str, Any]) -> None:
        cfg = config.get("artwork", {})
        self._timeout = float(cfg.get("lookup_timeout_s", 12))
        self._size = int(cfg.get("preferred_size", 1200))
        sources = cfg.get("sources") or ["itunes", "musicbrainz", "spotify", "audd"]
        self._sources = [str(s).lower() for s in sources]
        self._spotify_id = (
            os.getenv(str(cfg.get("spotify_client_id_env", "SPOTIFY_CLIENT_ID")), "")
            or str(cfg.get("spotify_client_id") or "")
        ).strip()
        self._spotify_secret = (
            os.getenv(str(cfg.get("spotify_client_secret_env", "SPOTIFY_CLIENT_SECRET")), "")
            or str(cfg.get("spotify_client_secret") or "")
        ).strip()
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": "AuraFrame/0.5 (https://github.com/DrHultner/auraframe)",
                "Accept": "application/json",
            }
        )
        self._spotify_token: str | None = None
        self._spotify_token_exp = 0.0
        self._mb_last_at = 0.0
        logger.info("Artwork lookup sources=%s size=%s", self._sources, self._size)

    def resolve(self, track: TrackInfo, preferred_url: str | None = None) -> ArtworkHit | None:
        hits: list[ArtworkHit] = []

        for source in self._sources:
            try:
                if source == "itunes":
                    hits.extend(self._itunes(track))
                elif source in {"musicbrainz", "mb", "coverartarchive", "caa"}:
                    hits.extend(self._musicbrainz(track))
                elif source == "spotify":
                    hits.extend(self._spotify(track))
                elif source == "audd" and preferred_url:
                    hits.append(ArtworkHit(url=preferred_url, source="audd", score=0.55, width=self._size))
            except Exception:
                logger.exception("Artwork lookup failed source=%s", source)

        if not hits and preferred_url:
            hits.append(ArtworkHit(url=preferred_url, source="audd", score=0.4, width=0))

        if not hits:
            logger.warning(
                "No artwork found for %s — %s (%s)",
                track.artist,
                track.title,
                track.album,
            )
            return None

        hits.sort(key=lambda h: (h.score, h.width), reverse=True)
        best = hits[0]
        logger.info(
            "Artwork chosen source=%s score=%.2f width=%s url=%s",
            best.source,
            best.score,
            best.width,
            best.url,
        )
        return best

    def _itunes(self, track: TrackInfo) -> list[ArtworkHit]:
        hits: list[ArtworkHit] = []
        queries = []
        if track.album and track.artist:
            queries.append((f"{track.artist} {track.album}", "album"))
        if track.artist and track.title:
            queries.append((f"{track.artist} {track.title}", "song"))
        if track.title:
            queries.append((track.title, "song"))

        seen_q: set[str] = set()
        for term, entity in queries:
            key = f"{entity}:{term.lower()}"
            if key in seen_q:
                continue
            seen_q.add(key)
            url = (
                "https://itunes.apple.com/search"
                f"?term={quote_plus(term)}&media=music&entity={entity}&limit=8"
            )
            resp = self._session.get(url, timeout=self._timeout)
            resp.raise_for_status()
            results = resp.json().get("results") or []
            for item in results:
                art = item.get("artworkUrl100") or item.get("artworkUrl60")
                if not art:
                    continue
                art = self._itunes_hires(str(art), self._size)
                artist = str(item.get("artistName") or "")
                title = str(item.get("trackName") or item.get("collectionName") or "")
                album = str(item.get("collectionName") or "")
                score = self._score_match(track, artist=artist, title=title, album=album)
                # Prefer album entities slightly when album known
                if entity == "album" and track.album:
                    score += 0.05
                hits.append(ArtworkHit(url=art, source="itunes", score=score, width=self._size))
        return hits

    def _musicbrainz(self, track: TrackInfo) -> list[ArtworkHit]:
        # MusicBrainz asks for <= 1 req/s
        elapsed = time.monotonic() - self._mb_last_at
        if elapsed < 1.05:
            time.sleep(1.05 - elapsed)

        parts = []
        if track.title:
            parts.append(f'recording:"{track.title}"')
        if track.artist:
            parts.append(f'artist:"{track.artist}"')
        if not parts:
            return []
        query = " AND ".join(parts)
        url = (
            "https://musicbrainz.org/ws/2/recording/"
            f"?query={quote_plus(query)}&fmt=json&limit=5"
        )
        resp = self._session.get(url, timeout=self._timeout)
        self._mb_last_at = time.monotonic()
        resp.raise_for_status()
        recordings = resp.json().get("recordings") or []
        hits: list[ArtworkHit] = []

        for rec in recordings:
            rec_title = str(rec.get("title") or "")
            artist_credit = rec.get("artist-credit") or []
            artist = " ".join(str(a.get("name") or "") for a in artist_credit if isinstance(a, dict))
            releases = rec.get("releases") or []
            base = self._score_match(track, artist=artist, title=rec_title, album="")
            for rel in releases[:3]:
                mbid = rel.get("id")
                if not mbid:
                    continue
                album = str(rel.get("title") or "")
                score = max(base, self._score_match(track, artist=artist, title=rec_title, album=album))
                cover = self._cover_art_archive(str(mbid))
                if cover:
                    hits.append(
                        ArtworkHit(
                            url=cover,
                            source="musicbrainz",
                            score=score,
                            width=self._size,
                        )
                    )
                    if score >= 0.85:
                        return hits
        return hits

    def _cover_art_archive(self, release_mbid: str) -> str | None:
        elapsed = time.monotonic() - self._mb_last_at
        if elapsed < 1.05:
            time.sleep(1.05 - elapsed)
        url = f"https://coverartarchive.org/release/{release_mbid}"
        try:
            resp = self._session.get(url, timeout=self._timeout, allow_redirects=True)
            self._mb_last_at = time.monotonic()
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            images = resp.json().get("images") or []
        except Exception:
            logger.debug("CAA miss for %s", release_mbid, exc_info=True)
            return None

        front = None
        for img in images:
            if img.get("front"):
                front = img
                break
        if front is None and images:
            front = images[0]
        if not front:
            return None
        thumbs = front.get("thumbnails") or {}
        # prefer large
        for key in (str(self._size), "1200", "1000", "500", "large", "small"):
            if key in thumbs and thumbs[key]:
                return str(thumbs[key])
        if front.get("image"):
            return str(front["image"])
        return None

    def _spotify(self, track: TrackInfo) -> list[ArtworkHit]:
        token = self._spotify_access_token()
        if not token:
            return []
        q_parts = []
        if track.title:
            q_parts.append(f'track:"{track.title}"')
        if track.artist:
            q_parts.append(f'artist:"{track.artist}"')
        if track.album:
            q_parts.append(f'album:"{track.album}"')
        q = " ".join(q_parts) or track.title or track.artist
        url = f"https://api.spotify.com/v1/search?q={quote_plus(q)}&type=track&limit=8"
        resp = self._session.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=self._timeout,
        )
        if resp.status_code == 401:
            self._spotify_token = None
            return []
        resp.raise_for_status()
        items = (((resp.json().get("tracks") or {}).get("items")) or [])
        hits: list[ArtworkHit] = []
        for item in items:
            album = item.get("album") or {}
            images = album.get("images") or []
            if not images:
                continue
            images = sorted(images, key=lambda im: int(im.get("width") or 0), reverse=True)
            img = images[0]
            artists = ", ".join(str(a.get("name") or "") for a in (item.get("artists") or []))
            score = self._score_match(
                track,
                artist=artists,
                title=str(item.get("name") or ""),
                album=str(album.get("name") or ""),
            )
            hits.append(
                ArtworkHit(
                    url=str(img.get("url")),
                    source="spotify",
                    score=score,
                    width=int(img.get("width") or 0),
                )
            )
        return hits

    def _spotify_access_token(self) -> str | None:
        if not self._spotify_id or not self._spotify_secret:
            return None
        now = time.time()
        if self._spotify_token and now < self._spotify_token_exp - 30:
            return self._spotify_token
        resp = self._session.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "client_credentials"},
            auth=(self._spotify_id, self._spotify_secret),
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        self._spotify_token = str(data.get("access_token") or "")
        self._spotify_token_exp = now + float(data.get("expires_in") or 3600)
        return self._spotify_token or None

    def _score_match(
        self,
        track: TrackInfo,
        *,
        artist: str,
        title: str,
        album: str,
    ) -> float:
        s_artist = _similarity(track.artist, artist)
        s_title = _similarity(track.title, title)
        s_album = _similarity(track.album, album) if track.album else 0.0
        # Weighted: title+artist matter most; album boosts when present
        if track.album:
            score = 0.35 * s_artist + 0.35 * s_title + 0.30 * s_album
        else:
            score = 0.45 * s_artist + 0.55 * s_title
        return float(score)

    @staticmethod
    def _itunes_hires(url: str, size: int) -> str:
        # artworkUrl100 → swap dimension suffix for larger art
        out = re.sub(r"\d+x\d+bb", f"{size}x{size}bb", url)
        out = re.sub(r"\d+x\d+cc", f"{size}x{size}bb", out)
        out = out.replace("100x100", f"{size}x{size}").replace("60x60", f"{size}x{size}")
        return out
