"""Tests for the source scanner: webpage crawling, direct URLs, yt-dlp paths."""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.services import source_scanner
from app.services.source_scanner import ScanError


PAGE_1 = """<html><body>
<video src="https://cdn.example.com/clip1.mp4"></video>
<a href="/page2.html">next</a>
<a href="https://other.example.com/external.mp4">ext</a>
</body></html>"""

PAGE_2 = """<html><body>
<a href="/media/deep_clip.webm">deep</a>
<a href="mailto:x@y.z">mail</a>
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/page1.html":
            body = PAGE_1.encode()
        elif self.path == "/page2.html":
            body = PAGE_2.encode()
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def web_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    thread.join(timeout=2)


@pytest.fixture()
def allow_local_scan(monkeypatch):
    """The SSRF guard blocks loopback by design; the crawler tests exercise
    scanning logic against a local test server, so bypass the guard here."""
    monkeypatch.setattr(source_scanner, "validate_public_url", lambda url, allowed=None: url)


def test_scan_webpage_extracts_videos(web_server, app_env, allow_local_scan):
    result = source_scanner.scan_webpage(f"{web_server}/page1.html", depth=1)
    urls = {v["url"] for v in result.videos}
    assert any(u.endswith("clip1.mp4") for u in urls)
    assert any(u.endswith("deep_clip.webm") for u in urls)
    assert any(u.endswith("external.mp4") for u in urls)  # direct video links are in scope
    assert result.pages >= 2


def test_scan_webpage_crawl_limits(web_server, app_env, allow_local_scan):
    result = source_scanner.scan_webpage(f"{web_server}/page1.html", depth=0)
    urls = {v["url"] for v in result.videos}
    assert any(u.endswith("clip1.mp4") for u in urls)
    assert not any(u.endswith("deep_clip.webm") for u in urls)  # depth 0


def test_scan_video_url(allow_local_scan):
    result = source_scanner.scan_video_url("https://cdn.example.com/a.mp4")
    assert result.videos[0]["url"].endswith("a.mp4")
    with pytest.raises(ScanError):
        source_scanner.scan_video_url("https://example.com/notavideo")


def test_scan_blocks_ssrf_and_disallowed_domains(app_env):
    with pytest.raises(ValueError):
        source_scanner.scan_webpage("http://127.0.0.1:8000/page")
    with pytest.raises(ValueError):
        source_scanner.scan_webpage("http://127.0.0.1/x.mp4")


def test_detect_source_kind():
    assert source_scanner.detect_source_kind("https://www.youtube.com/@x") == "youtube"
    assert source_scanner.detect_source_kind("https://www.twitch.tv/x") == "twitch"
    assert source_scanner.detect_source_kind("https://kick.com/x") == "kick"
    assert source_scanner.detect_source_kind("https://cdn.x.com/a.mp4") == "direct"
    assert source_scanner.detect_source_kind("https://example.com/article") == "webpage"


def test_ytdlp_extract_info_mocked(app_env, monkeypatch):
    # The extraction logic test; SSRF validation is patched through so the
    # fake URLs (which do not resolve) are not filtered out here.
    monkeypatch.setattr(source_scanner, "validate_public_url", lambda url, allowed=None: url)

    class _FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            return {
                "_type": "playlist",
                "title": "My Playlist",
                "entries": [
                    {"title": "Part 1", "webpage_url": "https://www.youtube.com/watch?v=v1"},
                    {"title": "Part 2", "webpage_url": "https://www.youtube.com/watch?v=v2"},
                ],
            }

    import yt_dlp

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _FakeYDL)
    info = source_scanner.ytdlp_extract_info("https://www.youtube.com/playlist?list=x")
    assert info["is_playlist"]
    assert len(info["videos"]) == 2


def test_ytdlp_extract_info_filters_unsafe_urls(app_env, monkeypatch):
    """yt-dlp resolved URLs pointing at private/loopback hosts are filtered out."""
    class _FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            return {
                "_type": "playlist",
                "title": "Mixed",
                "entries": [
                    {"title": "Public", "webpage_url": "https://www.youtube.com/watch?v=ok"},
                    {"title": "Private", "webpage_url": "http://127.0.0.1/secret"},
                ],
            }

    import yt_dlp

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _FakeYDL)
    info = source_scanner.ytdlp_extract_info("https://www.youtube.com/playlist?list=x")
    # only the public URL survives SSRF validation
    assert len(info["videos"]) == 1
    assert info["videos"][0]["url"].startswith("https://www.youtube.com/")


def test_ytdlp_failure(app_env, monkeypatch):
    import yt_dlp

    def _boom(self, url, download=False):
        raise yt_dlp.utils.DownloadError("blocked")

    monkeypatch.setattr(yt_dlp.YoutubeDL, "extract_info", _boom)
    with pytest.raises(ScanError):
        source_scanner.ytdlp_extract_info("https://www.youtube.com/@x")


def test_fetch_page_blocks_redirect_to_private(app_env, monkeypatch):
    """A redirect hop to a private/loopback address must be rejected (SSRF)."""
    import httpx

    class _RedirectResponse:
        status_code = 302
        headers = {"location": "http://127.0.0.1:8000/admin"}
        text = ""

    class _OKResponse:
        status_code = 200
        headers = {"content-type": "text/html"}
        text = "<html>ok</html>"

    def _fake_get(url, headers=None, follow_redirects=False, timeout=None):
        # first call redirects to loopback; must never be followed
        return _RedirectResponse()

    monkeypatch.setattr(httpx, "get", _fake_get)
    with pytest.raises(ValueError):
        source_scanner._fetch_page("https://example.com/start")


def test_fetch_page_blocks_redirect_to_metadata(app_env, monkeypatch):
    """Redirect to cloud-metadata endpoint must be rejected."""
    import httpx

    class _RedirectResponse:
        status_code = 301
        headers = {"location": "http://169.254.169.254/latest/meta-data/"}
        text = ""

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _RedirectResponse())
    with pytest.raises(ValueError):
        source_scanner._fetch_page("https://example.com/start")


def test_fetch_page_limits_redirects(app_env, monkeypatch):
    """Too many redirects raises ScanError instead of looping forever."""
    import httpx

    class _RedirectResponse:
        status_code = 302
        headers = {"location": "https://example.com/next"}
        text = ""

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _RedirectResponse())
    with pytest.raises(ScanError):
        source_scanner._fetch_page("https://example.com/start")


def test_fetch_page_rejects_non_html_non_video(app_env, monkeypatch):
    """Non-HTML, non-video content type is rejected."""
    import httpx

    class _Response:
        status_code = 200
        headers = {"content-type": "application/octet-stream"}
        text = "binary"

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Response())
    with pytest.raises(ScanError):
        source_scanner._fetch_page("https://example.com/data.bin")
