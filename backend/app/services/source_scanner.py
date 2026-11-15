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


class DownloadCancelled(ScanError):
    """The worker's cancellation signal interrupted an active import."""


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


def _fetch_page(url: str, timeout: float = 30.0, max_redirects: int = 5) -> str:
    headers = {
        "User-Agent": "ArcusClips/1.0 (self-hosted video clipping studio; +local)",
        "Accept": "text/html,application/xhtml+xml",
    }
    # Follow redirects manually so every hop is SSRF-validated before fetch.
    current = url
    for _ in range(max_redirects + 1):
        try:
            resp = httpx.get(current, headers=headers, follow_redirects=False, timeout=timeout)
        except httpx.HTTPError as exc:
            raise ScanError(f"Could not fetch {current}: {exc}") from exc
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location", "")
            if not location:
                raise ScanError(f"Redirect from {current} has no location")
            current = urljoin(current, location)
            # SSRF: validate the redirect destination before following it.
            current = validate_public_url(current, allowed_domains() or None)
            continue
        if resp.status_code >= 400:
            raise ScanError(f"HTTP {resp.status_code} fetching {current}")
        ctype = resp.headers.get("content-type", "")
        if "html" not in ctype and "text" not in ctype:
            # non-HTML resource: only accept if it looks like a video
            if not _is_video_url(current):
                raise ScanError(f"Unsupported content type {ctype} at {current}")
        return resp.text
    raise ScanError(f"Too many redirects fetching {url}")


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
                # SSRF: validate every extracted link before it is fetched.
                try:
                    link = validate_public_url(link, allowed_domains() or None)
                except ValueError as exc:
                    result.errors.append(f"Skipped unsafe link {link}: {exc}")
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

    from .ytdlp_guard import ByteBudget, GuardedYoutubeDL

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": True,
        "noplaylist": False,
        "ignoreerrors": True,
        "socket_timeout": 30,
        "hls_prefer_native": True,
        "external_downloader": "native",
    }

    def _hook(d: dict[str, Any]) -> None:
        if d.get("status") == "downloading" and progress_cb:
            progress_cb(0.5, f"Resolving {d.get('info_dict', {}).get('title', url)}")

    opts["progress_hooks"] = [_hook]
    budget = ByteBudget(settings.max_download_size or None)
    try:
        with GuardedYoutubeDL(opts, byte_budget=budget) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise ScanError(f"yt-dlp could not resolve this source: {exc}") from exc
    if not info:
        raise ScanError("yt-dlp returned no data for this source")
    # SSRF: validate every resolved URL before exposing it. yt-dlp may follow
    # redirects internally; we re-validate the final resolved URLs so private,
    # loopback and link-local destinations are never returned as importable.
    def _safe_url(u: str) -> str | None:
        if not u:
            return None
        try:
            return validate_public_url(u, allowed_domains() or None)
        except ValueError:
            return None

    if info.get("_type") == "playlist":
        entries = [e for e in info.get("entries", []) if e]
        items = []
        for e in entries:
            u = _safe_url(e.get("webpage_url") or e.get("url", ""))
            if not u:
                continue
            items.append({"url": u, "name": e.get("title", ""), "kind": "playlist_item"})
        return {"is_playlist": True, "title": info.get("title", ""), "videos": items}
    safe = _safe_url(info.get("webpage_url") or url)
    if not safe:
        raise ScanError("yt-dlp resolved to a non-public/unsafe URL; refusing to import")
    return {
        "is_playlist": False,
        "title": info.get("title", ""),
        "videos": [{"url": safe, "name": info.get("title", ""), "kind": "video"}],
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


# Protocols that are known to resolve http(s) URLs and to download through
# yt-dlp's native (in-process, SSRF-validated) downloaders. Anything else
# (rtmp, websocket, ffmpeg-handled live HLS, ...) is refused before download.
_ALLOWED_PROTOCOLS = frozenset({
    "http", "https",
    "m3u8", "m3u8_native", "m3u8_frag_urls",
    "http_dash_segments", "http_dash_segments_generator", "dash_frag_urls",
    "f4m", "ism",
})


def _snapshot_files(dest_dir) -> set:
    """All regular files under dest_dir (used to detect artifacts created by an attempt)."""
    from pathlib import Path

    dest = Path(dest_dir)
    if not dest.is_dir():
        return set()
    return {p for p in dest.rglob("*") if p.is_file()}


def _is_temp_artifact(path) -> bool:
    name = path.name
    return (
        name.endswith(".part")
        or name.endswith(".ytdl")
        or "-Frag" in name
        or name.endswith(".frag")
    )


def _cleanup_new_files(dest_dir, before: set, keep=(), temp_only: bool = False) -> list:
    """Remove files created after ``before`` was taken (except ``keep``).

    When ``temp_only`` is set, only yt-dlp temporary artifacts (.part, .ytdl,
    fragment files) are removed. Pre-existing (unrelated) files are never
    touched. Returns the list of removed paths.
    """
    from pathlib import Path

    keep_set = {Path(k).resolve() for k in keep}
    removed = []
    for p in _snapshot_files(dest_dir):
        if p in before:
            continue
        try:
            if p.resolve() in keep_set:
                continue
        except OSError:
            pass
        if temp_only and not _is_temp_artifact(p):
            continue
        try:
            p.unlink()
            removed.append(p)
        except OSError:
            logger.warning("Could not remove download artifact %s", p)
    return removed


def _iter_format_urls(info: dict[str, Any]):
    yield info.get("url")
    yield info.get("manifest_url")
    formats = list(info.get("formats") or []) + list(info.get("requested_formats") or [])
    for fmt in formats:
        if isinstance(fmt, dict):
            yield fmt.get("url")
    for group in (info.get("subtitles") or {}).values():
        for sub in group or []:
            if isinstance(sub, dict):
                yield sub.get("url")
    for thumb in info.get("thumbnails") or []:
        if isinstance(thumb, dict):
            yield thumb.get("url")


def _vet_extracted_info(info: dict[str, Any]) -> None:
    """Vet everything yt-dlp resolved *before* any media bytes are downloaded.

    Raises ScanError for live streams and for URLs/protocols that cannot be
    handled by the SSRF-validated native download path.
    """
    if info.get("is_live"):
        raise ScanError(
            "Live streams are not supported for import (they are unbounded in size and duration)"
        )
    protocols = {info.get("protocol")}
    for fmt in list(info.get("formats") or []) + list(info.get("requested_formats") or []):
        if isinstance(fmt, dict):
            protocols.add(fmt.get("protocol"))
            if fmt.get("is_live"):
                raise ScanError(
                    "Live streams are not supported for import (they are unbounded in size and duration)"
                )
    for proto in {p for p in protocols if p}:
        for sp in str(proto).split("+"):
            if sp.strip() not in _ALLOWED_PROTOCOLS:
                raise ScanError(f"Unsupported protocol for import: {sp}")
    for raw in _iter_format_urls(info):
        if not raw:
            continue
        try:
            validate_public_url(raw)
        except ValueError as exc:
            raise ScanError(f"Refusing to download unsafe media URL: {exc}") from exc


def _downloaded_filepaths(info: dict[str, Any], dest: "Path", before: set) -> list:
    paths = []
    for rd in info.get("requested_downloads") or []:
        fp = rd.get("filepath") if isinstance(rd, dict) else None
        if fp:
            paths.append(fp)
    if not paths and info.get("_filename"):
        paths.append(info["_filename"])
    if not paths:
        # Fallback: newest non-temp file created during this attempt
        created = [p for p in (_snapshot_files(dest) - before) if not _is_temp_artifact(p)]
        if created:
            newest = max(created, key=lambda p: p.stat().st_mtime)
            paths.append(str(newest))
    return paths


def import_video_from_url(url: str, dest_dir: str, progress_cb: ProgressCb = None,
                          cancel_event=None) -> dict[str, Any]:
    """Download a single supported video URL to dest_dir via yt-dlp.

    Every outbound request yt-dlp makes (metadata, media, fragments, redirects)
    is SSRF-validated at connect time; the running byte budget enforces
    ARCUS_MAX_DOWNLOAD_SIZE even without provider size info; the worker's
    cancellation signal aborts the active transfer; and partial files/artifacts
    created by a failed or rejected attempt are removed (unrelated files in
    dest_dir are never touched).
    """
    if not ytdlp_available():
        raise ScanError("yt-dlp is not installed")
    try:
        url = validate_public_url(url, allowed_domains() or None)
    except ValueError as exc:
        raise ScanError(str(exc)) from exc
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled("Download cancelled")

    from pathlib import Path

    import yt_dlp

    from .ytdlp_guard import (
        ByteBudget,
        DownloadLimitExceeded,
        GuardedYoutubeDL,
        SsrfBlockedError,
    )

    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    before = _snapshot_files(dest)

    max_bytes = settings.max_download_size or 0
    budget = ByteBudget(max_bytes if max_bytes > 0 else None)

    opts = {
        "outtmpl": _dest_template(str(dest)),
        "quiet": True,
        "no_warnings": True,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        # No max_filesize: it is a silent early-bail (not an exception), which
        # would route around the running byte budget. The budget and the final
        # on-disk check below are the authoritative size enforcement.
        "retries": 3,
        "fragment_retries": 3,
        # Keep all media/fragment traffic on the in-process, SSRF-validated
        # native download path (never an external downloader subprocess).
        "hls_prefer_native": True,
        "external_downloader": "native",
        "noplaylist": True,
    }

    def _hook(d: dict[str, Any]) -> None:
        if cancel_event is not None and cancel_event.is_set():
            # yt_dlp's native cancellation exception: re-raised untouched by
            # yt-dlp's extraction wrapper, so the download aborts promptly.
            raise yt_dlp.utils.DownloadCancelled("Download cancelled")
        if progress_cb and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            if total:
                progress_cb(min(0.99, done / total), "Downloading…")

    opts["progress_hooks"] = [_hook]

    def _fail_cleanup() -> None:
        _cleanup_new_files(dest, before, keep=())

    try:
        with GuardedYoutubeDL(opts, byte_budget=budget) as ydl:
            info = ydl.extract_info(url, download=False)
            if info is None:
                raise ScanError("yt-dlp returned no data for this URL")
            # Vet everything that would be downloaded BEFORE the first media
            # request is made (formats, manifests, subtitles, thumbnails).
            _vet_extracted_info(info)
            info = ydl.process_ie_result(info, download=True)
    except (yt_dlp.utils.DownloadCancelled,) as exc:
        _fail_cleanup()
        raise DownloadCancelled("Download cancelled") from exc
    except DownloadLimitExceeded as exc:
        _fail_cleanup()
        raise ScanError(f"Download exceeded max download size ({exc})") from exc
    except SsrfBlockedError as exc:
        _fail_cleanup()
        raise ScanError(f"Download blocked: {exc}") from exc
    except ScanError:
        _fail_cleanup()
        raise
    except Exception as exc:
        _fail_cleanup()
        raise ScanError(f"Download failed: {exc}") from exc

    if not info:
        _fail_cleanup()
        raise ScanError("yt-dlp returned no data for this URL")

    # Final on-disk size enforcement — independent of provider-reported sizes.
    filepaths = _downloaded_filepaths(info, dest, before)
    existing = [Path(fp) for fp in filepaths if Path(fp).is_file()]
    if not existing:
        _fail_cleanup()
        raise ScanError("yt-dlp did not produce an output file")
    total = sum(p.stat().st_size for p in existing)
    if max_bytes > 0 and total > max_bytes:
        _fail_cleanup()
        raise ScanError(f"Downloaded media exceeds max download size ({total} > {max_bytes} bytes)")

    # Success: drop temp artifacts created by this attempt; keep the final
    # output(s) and anything that existed before we started.
    _cleanup_new_files(dest, before, keep=existing, temp_only=True)

    return {
        "path": str(existing[0]),
        "title": info.get("title", ""),
        "url": info.get("webpage_url", url),
    }


def _dest_template(dest_dir: str) -> str:
    from pathlib import Path

    return str(Path(dest_dir) / "%(title).80B [%(id)s].%(ext)s")
