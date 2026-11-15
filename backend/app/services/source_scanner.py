"""Source connectors: public webpage scanning, direct video URLs, and
yt-dlp-backed channels/playlists.

The crawler is deliberately controlled: only explicitly supplied URLs, domain
allowlist + depth/page limits, deduplicated, redirect-safe, and never
bypassing authentication, paywalls, captchas, DRM or platform restrictions.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from ..config import settings
from ..security import validate_public_url

logger = logging.getLogger("arcusclips.sources")

ProgressCb = Any

_VIDEO_SUFFIXES = settings.supported_video_suffixes

# Patterns for embedded players / direct video references in HTML
_EMBED_PATTERNS = [
    r'<video[^>]+src=["\']([^"\']+)["\']',
    r'<source[^>]+src=["\']([^"\']+)["\']',
    r'<embed[^>]+src=["\']([^"\']+)["\']',
    r'<iframe[^>]+src=["\']([^"\']+)["\']',
    r'data-video=["\']([^"\']+)["\']',
    r'data-src=["\']([^"\']+\.(?:mp4|mov|mkv|webm|m4v|avi|mpeg|mpg|ts)(?:\?[^"\']*)?)["\']',
]


class ScanError(RuntimeError):
    pass


def allowed_domains() -> list[str]:
    raw = settings.allowed_source_domains or ""
    return [d.strip().lower() for d in raw.split(",") if d.strip()]


def _is_video_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith("." + ext) for ext in _VIDEO_SUFFIXES)


def _normalize_url(url: str, base: str | None = None) -> str:
    if base:
        url = urljoin(base, url)
    return url


# ---------------------------------------------------------------------------
@dataclass
class ScanResult:
    videos: list[dict[str, Any]] = field(default_factory=list)
    pages: int = 0
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _fetch_page(url: str, timeout: float = 30.0) -> str:
    headers = {
        "User-Agent": "ArcusClips/1.0 (self-hosted video clipping studio; +local)",
        "Accept": "text/html,application/xhtml+xml",
    }
    try:
        resp = httpx.get(url, headers=headers, follow_redirects=True, timeout=timeout)
    except httpx.HTTPError as exc:
        raise ScanError(f"Could not fetch {url}: {exc}") from exc
    if resp.status_code >= 400:
        raise ScanError(f"HTTP {resp.status_code} fetching {url}")
    ctype = resp.headers.get("content-type", "")
    if "html" not in ctype and "text" not in ctype:
        # non-HTML resource: only accept if it looks like a video
        if not _is_video_url(url):
            raise ScanError(f"Unsupported content type {ctype} at {url}")
    return resp.text


def _extract_videos_from_html(html: str, base_url: str) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for pattern in _EMBED_PATTERNS:
        for m in re.finditer(pattern, html, flags=re.I):
            raw = m.group(1)
            url = _normalize_url(raw, base_url)
            if _is_video_url(url) or urlparse(url).hostname == urlparse(base_url).hostname:
                if url not in found:
                    found[url] = {"url": url, "name": url.rsplit("/", 1)[-1], "kind": "embedded"}
    # Direct <a href> links to video files
    for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\']', html, flags=re.I):
        url = _normalize_url(m.group(1), base_url)
        if _is_video_url(url) and url not in found:
            found[url] = {"url": url, "name": url.rsplit("/", 1)[-1], "kind": "linked"}
    return list(found.values())


def scan_webpage(
    url: str,
    depth: int | None = None,
    max_pages: int | None = None,
    progress_cb: ProgressCb = None,
) -> ScanResult:
    """Scan a public webpage for embedded/linked video content, with a
    controlled same-domain crawl up to the configured depth."""
    url = validate_public_url(url, allowed_domains() or None)
    depth = settings.crawl_max_depth if depth is None else depth
    max_pages = settings.crawl_max_pages if max_pages is None else max_pages
    base_domain = urlparse(url).netloc.lower()

    result = ScanResult()
    visited: set[str] = set()
    queue: list[tuple[str, int]] = [(url, 0)]
    delay = settings.crawl_rate_limit_delay

    while queue and len(visited) < max_pages:
        current, d = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        try:
            if progress_cb:
                progress_cb(len(visited) / max(1, max_pages), f"Fetching {current}")
            html = _fetch_page(current)
        except ScanError as exc:
            result.errors.append(str(exc))
            continue
        result.pages += 1
        found = _extract_videos_from_html(html, current)
        existing = {v["url"] for v in result.videos}
        for v in found:
            if v["url"] not in existing:
                result.videos.append(v)
                existing.add(v["url"])
        # crawl same-domain links within depth limit
        if d < depth:
            for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\']', html, flags=re.I):
                link = _normalize_url(m.group(1), current)
                parsed = urlparse(link)
                if parsed.netloc.lower() != base_domain:
                    continue  # do not leave the configured domain
                if link.startswith("#") or link.startswith("mailto:"):
                    continue
                if link not in visited and link not in {q[0] for q in queue}:
                    queue.append((link, d + 1))
    return result


def scan_video_url(url: str) -> ScanResult:
    """A single direct video URL — validated and classified."""
    url = validate_public_url(url, allowed_domains() or None)
    if not _is_video_url(url):
        # Allow known video hosts regardless of suffix; yt-dlp decides at import.
        host = urlparse(url).netloc.lower()
        if not any(h in host for h in ("youtube.com", "youtu.be", "twitch.tv", "kick.com", "vimeo.com")):
            raise ScanError("URL does not point to a recognizable video")
    result = ScanResult()
    result.videos.append({
        "url": url,
        "name": url.rsplit("/", 1)[-1] or "video",
        "kind": "direct",
    })
    return result


# ---------------------------------------------------------------------------
# yt-dlp based connectors (channels / playlists / supported platforms)
# ---------------------------------------------------------------------------
def ytdlp_available() -> bool:
    try:
        import yt_dlp  # noqa: F401
        return True
    except ImportError:
        return False


def ytdlp_extract_info(url: str, progress_cb: ProgressCb = None) -> dict[str, Any]:
    """Use yt-dlp to enumerate a channel/playlist/video entry list.

    Only runs where the source and account permissions permit access;
    never bypasses platform restrictions.
    """
    if not ytdlp_available():
        raise ScanError("yt-dlp is not installed")
    url = validate_public_url(url, allowed_domains() or None)
    import yt_dlp

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "noplaylist": False,
        "ignoreerrors": True,
        "socket_timeout": 30,
    }

    def _hook(d: dict[str, Any]) -> None:
        if d.get("status") == "downloading" and progress_cb:
            progress_cb(0.5, f"Resolving {d.get('info_dict', {}).get('title', url)}")

    opts["progress_hooks"] = [_hook]
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise ScanError(f"yt-dlp could not resolve this source: {exc}") from exc
    if not info:
        raise ScanError("yt-dlp returned no data for this source")
    if info.get("_type") == "playlist":
        entries = [e for e in info.get("entries", []) if e]
        items = [
            {"url": e.get("webpage_url") or e.get("url", ""), "name": e.get("title", ""), "kind": "playlist_item"}
            for e in entries
        ]
        return {"is_playlist": True, "title": info.get("title", ""), "videos": items}
    return {
        "is_playlist": False,
        "title": info.get("title", ""),
        "videos": [{"url": info.get("webpage_url") or url, "name": info.get("title", ""), "kind": "video"}],
    }


def detect_source_kind(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if any(h in host for h in ("youtube.com", "youtu.be")):
        return "youtube"
    if "twitch.tv" in host:
        return "twitch"
    if "kick.com" in host:
        return "kick"
    if _is_video_url(url):
        return "direct"
    return "webpage"


def import_video_from_url(url: str, dest_dir: str, progress_cb: ProgressCb = None,
                          cancel_event=None) -> dict[str, Any]:
    """Download a single supported video URL to dest_dir via yt-dlp."""
    if not ytdlp_available():
        raise ScanError("yt-dlp is not installed")
    url = validate_public_url(url, allowed_domains() or None)
    import yt_dlp

    opts = {
        "outtmpl": _dest_template(dest_dir),
        "quiet": True,
        "no_warnings": True,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "max_filesize": settings.max_download_size,
        "retries": 3,
        "fragment_retries": 3,
    }

    def _hook(d: dict[str, Any]) -> None:
        if progress_cb and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            if total:
                progress_cb(min(0.99, done / total), "Downloading…")

    opts["progress_hooks"] = [_hook]

    class _Cancel(yt_dlp.utils.DownloadError):
        pass

    if cancel_event is not None:
        class _CancelController:
            def __init__(self):
                self._cancelled = False

        controller = _CancelController()

        def _check_cancel(_d):
            if cancel_event.is_set():
                raise _Cancel("cancelled")

        opts["progress_hooks"].append(_check_cancel)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as exc:
        raise ScanError(f"Download failed: {exc}") from exc
    expected = info.get("requested_downloads", [{}])[0].get("filepath") if info.get("requested_downloads") else None
    return {
        "path": expected or "",
        "title": info.get("title", ""),
        "url": info.get("webpage_url", url),
    }


def _dest_template(dest_dir: str) -> str:
    from pathlib import Path

    return str(Path(dest_dir) / "%(title).80B [%(id)s].%(ext)s")
