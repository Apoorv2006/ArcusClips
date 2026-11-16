"""Regression tests for yt-dlp SSRF/download-limit/cancellation enforcement.

These tests exercise the real production path used by ``import_video_from_url``:
``GuardedYoutubeDL`` -> ``GuardedUrllibRH`` -> ``GuardedHTTPHandler`` ->
``guarded_create_connection`` + ``GuardedResponse`` byte accounting.

The sandbox/CI cannot dial the public internet, so the test suite stands a
local HTTP server in for the public host and redirects the *transport seam*
(``socket.getaddrinfo`` for the fake host, plus ``ytdlp_guard._socket_connect``
to dial 127.0.0.1) at that host. Everything else runs for real: hostname
validation, resolution checks, redirect validation, byte budgeting, fragment
aggregation, cleanup, cancellation, and final on-disk verification.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import app.config
import app.services.source_scanner as source_scanner
import app.services.ytdlp_guard as ytdlp_guard
from app.services import source_scanner

PAYLOAD = b"0123456789abcdef" * 1000  # 16 KB
SEGMENT = b"\x47" * 8192  # 8 KB fake TS segment
PLAYLIST = (
    b"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-VERSION:3\n"
    b"#EXTINF:4.0,\nseg1.ts\n"
    b"#EXTINF:4.0,\nseg2.ts\n"
    b"#EXTINF:4.0,\nseg3.ts\n"
    b"#EXT-X-ENDLIST\n"
)


class _Handler(BaseHTTPRequestHandler):
    hits: dict[str, int] = {}
    throttle = 0.0

    def log_message(self, *args):  # silence
        pass

    def _hit(self, path: str) -> None:
        type(self).hits[path] = type(self).hits.get(path, 0) + 1

    def do_GET(self):  # noqa: N802 (http.server naming)
        path = self.path
        if path == "/video.mp4":
            self._hit(path)
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            self._stream_body(PAYLOAD)
        elif path == "/stream-no-length":
            self._hit(path)
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            # deliberately no Content-Length (unknown length)
            self.end_headers()
            self._stream_body(PAYLOAD)
        elif path == "/redirect-loopback":
            self._hit(path)
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/secret")
            self.end_headers()
        elif path == "/secret":
            self._hit(path)
            self.send_response(200)
            self.send_header("Content-Length", "6")
            self.end_headers()
            self.wfile.write(b"secret")
        elif path == "/stream.m3u8":
            self._hit(path)
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(PLAYLIST)))
            self.end_headers()
            self.wfile.write(PLAYLIST)
        elif path.endswith(".ts"):
            self._hit(path)
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Content-Length", str(len(SEGMENT)))
            self.end_headers()
            self.wfile.write(SEGMENT)
        else:
            self.send_response(404)
            self.end_headers()

    def _stream_body(self, body: bytes) -> None:
        if self.throttle > 0:
            for offset in range(0, len(body), 4096):
                try:
                    self.wfile.write(body[offset:offset + 4096])
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                time.sleep(self.throttle)
        else:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass


@pytest.fixture()
def public_server(monkeypatch):
    """Local HTTP server standing in for a public host.

    ``getaddrinfo`` reports a public IP for the fake host (so the SSRF checks
    pass), while the transport seam dials the local server. Loopback traffic
    and non-public resolutions are NOT patched — those must be rejected.
    """
    handler = _Handler
    handler.hits = {}
    handler.throttle = 0.0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    real_gai = socket.getaddrinfo

    def fake_getaddrinfo(host, port_requested, *args, **kwargs):
        try:
            ipaddress.ip_address(host)
            return real_gai(host, port_requested, *args, **kwargs)
        except ValueError:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     ("93.184.216.34", port_requested))]

    real_connect = ytdlp_guard._socket_connect

    def dial_local(info, timeout, source_address):
        return real_connect((socket.AF_INET, socket.SOCK_STREAM, 6, "",
                             ("127.0.0.1", info[4][1])), timeout, source_address)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(ytdlp_guard, "_socket_connect", dial_local)

    yield srv, port, handler.hits

    srv.shutdown()


@pytest.fixture()
def max_download_size():
    old = app.config.settings.max_download_size
    yield lambda v: setattr(app.config.settings, "max_download_size", v)
    app.config.settings.max_download_size = old


def _dest_files(path: str):
    return sorted(os.listdir(path)) if os.path.isdir(path) else []


# ---------------------------------------------------------------------------
# A. Unsafe destinations are rejected before/at the network boundary
# ---------------------------------------------------------------------------

def test_loopback_url_rejected_without_network_traffic(public_server, tmp_path):
    _, port, hits = public_server
    with pytest.raises((ValueError, source_scanner.ScanError)):
        source_scanner.import_video_from_url(f"http://127.0.0.1:{port}/video.mp4", str(tmp_path))
    assert hits.get("/video.mp4", 0) == 0


def test_private_dns_answer_rejected_without_network_traffic(public_server, tmp_path, monkeypatch):
    real_gai = socket.getaddrinfo

    def fake_getaddrinfo(host, port_requested, *args, **kwargs):
        if host == "media.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port_requested))]
        return real_gai(host, port_requested, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    _, port, hits = public_server
    with pytest.raises(source_scanner.ScanError):
        source_scanner.import_video_from_url(f"http://media.test:{port}/video.mp4", str(tmp_path))
    assert hits.get("/video.mp4", 0) == 0


def test_mixed_public_private_answer_rejected_without_network_traffic(public_server, tmp_path, monkeypatch):
    real_gai = socket.getaddrinfo

    def fake_getaddrinfo(host, port_requested, *args, **kwargs):
        if host == "media.test":
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port_requested)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port_requested)),
            ]
        return real_gai(host, port_requested, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    _, port, hits = public_server
    with pytest.raises(source_scanner.ScanError):
        source_scanner.import_video_from_url(f"http://media.test:{port}/video.mp4", str(tmp_path))
    assert hits.get("/video.mp4", 0) == 0


def test_guarded_connection_never_dials_when_validation_fails():
    attempts = []

    def sentinel_create(info, timeout, source_address):
        attempts.append(info)
        raise AssertionError("socket must not be created for prohibited destinations")

    import socket as _s
    real_gai = _s.getaddrinfo

    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host == "evil.test":
            return [(_s.AF_INET, _s.SOCK_STREAM, 6, "", ("192.168.1.1", port))]
        return real_gai(host, port, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_s, "getaddrinfo", fake_getaddrinfo)
        with pytest.raises(ytdlp_guard.SsrfBlockedError):
            ytdlp_guard.guarded_create_connection(
                ("evil.test", 80), 5, None, _create_socket_func=sentinel_create
            )
    assert attempts == []


def test_redirect_to_loopback_is_rejected_and_never_fetched(public_server, tmp_path):
    _, port, hits = public_server
    with pytest.raises(source_scanner.ScanError) as exc:
        source_scanner.import_video_from_url(f"http://media.test:{port}/redirect-loopback", str(tmp_path))
    assert "redirect" in str(exc.value).lower()
    assert hits.get("/redirect-loopback", 0) == 1
    assert hits.get("/secret", 0) == 0


def test_unsafe_media_url_in_extracted_info_rejected_before_download(tmp_path):
    info = {
        "_type": "video",
        "title": "x",
        "protocol": "https",
        "url": "https://media.test/video.mp4",
        "formats": [{"url": "http://127.0.0.1/internal", "protocol": "https"}],
    }
    with pytest.raises(source_scanner.ScanError):
        source_scanner._vet_extracted_info(info)


def test_live_stream_info_rejected_before_download():
    info = {"_type": "video", "protocol": "m3u8_native", "is_live": True,
            "url": "https://media.test/live.m3u8"}
    with pytest.raises(source_scanner.ScanError):
        source_scanner._vet_extracted_info(info)


def test_unsupported_protocol_rejected_before_download():
    info = {"_type": "video", "protocol": "rtmp",
            "url": "rtmp://media.test/live/stream"}
    with pytest.raises(source_scanner.ScanError):
        source_scanner._vet_extracted_info(info)


# ---------------------------------------------------------------------------
# B. Running byte budget enforcement
# ---------------------------------------------------------------------------

def test_successful_download_below_limit(public_server, tmp_path, max_download_size):
    _, port, hits = public_server
    max_download_size(1_000_000)
    res = source_scanner.import_video_from_url(f"http://media.test:{port}/video.mp4", str(tmp_path))
    path = res["path"]
    assert os.path.getsize(path) == len(PAYLOAD)
    # success leaves no temp artifacts behind
    leftover = _dest_files(str(tmp_path))
    assert all(not name.endswith((".part", ".ytdl")) for name in leftover)


def test_unknown_content_length_exceeding_limit(public_server, tmp_path, max_download_size):
    _, port, hits = public_server
    max_download_size(100)
    with pytest.raises(source_scanner.ScanError):
        source_scanner.import_video_from_url(f"http://media.test:{port}/stream-no-length", str(tmp_path))
    assert _dest_files(str(tmp_path)) == []


def test_fragmented_download_aggregate_limit(public_server, tmp_path, max_download_size):
    _, port, hits = public_server
    max_download_size(16 * 1024)  # 3 x 8 KB segments = 24 KB aggregate > limit
    with pytest.raises(source_scanner.ScanError):
        source_scanner.import_video_from_url(f"http://media.test:{port}/stream.m3u8", str(tmp_path))
    assert _dest_files(str(tmp_path)) == []


def test_fragmented_download_below_limit(public_server, tmp_path, max_download_size):
    _, port, hits = public_server
    max_download_size(1_000_000)
    res = source_scanner.import_video_from_url(f"http://media.test:{port}/stream.m3u8", str(tmp_path))
    assert os.path.getsize(res["path"]) > 0


def test_final_on_disk_size_check_enforces_limit(public_server, tmp_path, max_download_size, monkeypatch):
    """Defense-in-depth: even if the streaming budget is bypassed, the final
    on-disk size check rejects oversize output and cleans it up."""
    _, port, hits = public_server
    max_download_size(100)

    class _PassThroughBudget:
        def __init__(self, limit=None):
            self.limit = None
            self.used = 0

        def consume(self, n):
            self.used += n

    monkeypatch.setattr(ytdlp_guard, "ByteBudget", _PassThroughBudget)
    # source_scanner imports ByteBudget from ytdlp_guard at call time
    with pytest.raises(source_scanner.ScanError) as exc:
        source_scanner.import_video_from_url(f"http://media.test:{port}/video.mp4", str(tmp_path))
    assert "exceeds max download size" in str(exc.value).lower()
    assert _dest_files(str(tmp_path)) == []


def test_unrelated_files_in_dest_dir_are_preserved(public_server, tmp_path, max_download_size):
    keep = tmp_path / "keep.txt"
    keep.write_text("do not delete")
    _, port, hits = public_server
    max_download_size(100)
    with pytest.raises(source_scanner.ScanError):
        source_scanner.import_video_from_url(f"http://media.test:{port}/video.mp4", str(tmp_path))
    assert keep.read_text() == "do not delete"
    # only the unrelated file remains
    assert _dest_files(str(tmp_path)) == ["keep.txt"]


# ---------------------------------------------------------------------------
# C. Cancellation
# ---------------------------------------------------------------------------

def test_cancellation_before_start_stops_download(public_server, tmp_path):
    cancel = threading.Event()
    cancel.set()
    _, port, hits = public_server
    with pytest.raises(source_scanner.DownloadCancelled):
        source_scanner.import_video_from_url(f"http://media.test:{port}/video.mp4",
                                             str(tmp_path), cancel_event=cancel)
    assert hits.get("/video.mp4", 0) == 0


def test_cancellation_during_download_aborts_and_cleans(public_server, tmp_path):
    handler = _Handler
    handler.throttle = 0.05
    _, port, hits = public_server
    cancel = threading.Event()
    result: dict[str, object] = {}

    def run():
        try:
            result["res"] = source_scanner.import_video_from_url(
                f"http://media.test:{port}/video.mp4", str(tmp_path),
                progress_cb=lambda p, m: cancel.set(),  # cancel as soon as bytes flow
                cancel_event=cancel,
            )
        except Exception as exc:  # noqa: BLE001 - assert on the exception below
            result["exc"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=15)
    handler.throttle = 0.0
    assert not thread.is_alive(), "download did not observe cancellation promptly"
    exc = result.get("exc")
    assert isinstance(exc, source_scanner.DownloadCancelled)
    assert _dest_files(str(tmp_path)) == []


# ---------------------------------------------------------------------------
# D. Format vetting: storyboard/image-only tracks vs. selected media streams
# ---------------------------------------------------------------------------

@pytest.fixture()
def fake_public_dns(monkeypatch):
    """Hostnames resolve to a public IP; literal IPs resolve for real."""
    import ipaddress
    import socket as _s

    real_gai = _s.getaddrinfo

    def fake_gai(host, port, *args, **kwargs):
        try:
            ipaddress.ip_address(host)
            return real_gai(host, port, *args, **kwargs)
        except ValueError:
            return [(_s.AF_INET, _s.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(_s, "getaddrinfo", fake_gai)


def _storyboard_formats():
    return [
        {
            "format_id": f"sb{i}",
            "format_note": f"storyboard {i + 1}",
            "ext": "mhtml",
            "protocol": "mhtml",
            "vcodec": "none",
            "acodec": "none",
            "url": f"https://i.ytimg.com/sb/video/storyboard{i}.jpg?sqp=x",
            "filesize": 1234,
        }
        for i in range(4)
    ]


def _media_formats():
    video = {
        "format_id": "137",
        "ext": "mp4",
        "protocol": "https",
        "vcodec": "avc1.640028",
        "acodec": "none",
        "url": "https://rr2---sn-abc.googlevideo.com/videoplayback?id=video",
        "filesize": 1_000_000,
    }
    audio = {
        "format_id": "140",
        "ext": "m4a",
        "protocol": "https",
        "vcodec": "none",
        "acodec": "mp4a.40.2",
        "url": "https://rr2---sn-abc.googlevideo.com/videoplayback?id=audio",
        "filesize": 100_000,
    }
    return video, audio


def _youtube_like_info():
    """Mirror a YouTube extraction result *after* the format selector ran:
    storyboards stay in info['formats'] unused; the merged selection is
    top-level protocol 'https+https' and the components under
    'requested_formats'."""
    video, audio = _media_formats()
    return {
        "_type": "video",
        "id": "abc123",
        "title": "Some video",
        "protocol": "https+https",
        "ext": "mp4",
        "vcodec": video["vcodec"],
        "acodec": audio["acodec"],
        "format": "137+140",
        "format_id": "137+140",
        # merged selection: no top-level url (yt-dlp sets it only in the download path)
        "formats": [video, audio, *_storyboard_formats()],
        "requested_formats": [dict(video), dict(audio)],
        "webpage_url": "https://www.youtube.com/watch?v=abc123",
    }


def test_mhtml_not_in_allowed_protocols():
    assert "mhtml" not in source_scanner._ALLOWED_PROTOCOLS


def test_youtube_like_info_with_storyboards_passes_validation(fake_public_dns):
    info = _youtube_like_info()  # storyboards have protocol=mhtml, not selected
    source_scanner._vet_extracted_info(info)  # must not raise


def test_storyboard_formats_detected_by_metadata():
    for sb in _storyboard_formats():
        assert source_scanner._is_storyboard_format(sb)
    video, audio = _media_formats()
    assert not source_scanner._is_storyboard_format(video)
    assert not source_scanner._is_storyboard_format(audio)
    # storyboard identified by format_note even without the mhtml ext/protocol
    assert source_scanner._is_storyboard_format(
        {"format_id": "sb5", "format_note": "Storyboard 5", "ext": "webp",
         "protocol": "https", "vcodec": "none", "acodec": "none"})
    # a real webp video must not be treated as a storyboard
    assert not source_scanner._is_storyboard_format(
        {"format_id": "1", "ext": "webp", "protocol": "https",
         "vcodec": "vp9", "acodec": "none", "url": "https://cdn.test/x.webp"})


def test_storyboard_only_selection_rejected_as_non_media():
    info = _youtube_like_info()
    info["requested_formats"] = [dict(_storyboard_formats()[0])]
    with pytest.raises(source_scanner.ScanError, match="non-media"):
        source_scanner._vet_extracted_info(info)


def test_selected_unsupported_media_protocol_rejected():
    info = _youtube_like_info()
    video = dict(info["requested_formats"][0])
    video["protocol"] = "rtmp"
    info["requested_formats"] = [video, info["requested_formats"][1]]
    with pytest.raises(source_scanner.ScanError, match="Unsupported protocol"):
        source_scanner._vet_extracted_info(info)


@pytest.mark.parametrize("bad_url", [
    "http://127.0.0.1:8080/secret.mp4",
    "http://[::1]/secret.mp4",
    "http://192.168.1.50/secret.mp4",
    "http://169.254.169.254/meta.mp4",
])
def test_selected_media_url_private_or_loopback_rejected(bad_url):
    info = _youtube_like_info()
    video = dict(info["requested_formats"][0])
    video["url"] = bad_url
    info["requested_formats"] = [video, info["requested_formats"][1]]
    with pytest.raises(source_scanner.ScanError):
        source_scanner._vet_extracted_info(info)


def test_info_with_no_resolved_stream_rejected():
    with pytest.raises(source_scanner.ScanError, match="did not resolve"):
        source_scanner._vet_extracted_info({"_type": "video", "id": "x", "title": "x"})


def test_import_with_youtube_like_info_and_storyboards_succeeds(tmp_path, monkeypatch):
    """The full import path: vetting must pass for a YT-like extraction whose
    only unsupported entries (mhtml storyboards) are not selected, then the
    download proceeds through the (fake) guarded path."""
    import copy
    from pathlib import Path

    info = _youtube_like_info()
    writes = []

    class _FakeYdl:
        def __init__(self, opts, byte_budget=None):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            return copy.deepcopy(info)

        def process_ie_result(self, data, download=True):
            out = Path(tmp_path) / "Some video [abc123].mp4"
            out.write_bytes(PAYLOAD)
            writes.append(out)
            merged = dict(data)
            merged["requested_downloads"] = [{"filepath": str(out)}]
            return merged

    monkeypatch.setattr(ytdlp_guard, "GuardedYoutubeDL", _FakeYdl)
    monkeypatch.setattr(source_scanner, "validate_public_url", lambda u, allowed=None: u)

    res = source_scanner.import_video_from_url(
        "https://www.youtube.com/watch?v=abc123", str(tmp_path))
    assert Path(res["path"]).is_file()
    assert Path(res["path"]).stat().st_size == len(PAYLOAD)
    assert len(writes) == 1


def test_import_with_selected_unsupported_protocol_fails_before_download(tmp_path, monkeypatch):
    """An actually selected rtmp stream must abort before any media bytes move."""
    import copy

    info = _youtube_like_info()
    info["requested_formats"][0]["protocol"] = "rtmp"
    writes = []

    class _FakeYdl:
        def __init__(self, opts, byte_budget=None):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            return copy.deepcopy(info)

        def process_ie_result(self, data, download=True):
            writes.append(True)
            return data

    monkeypatch.setattr(ytdlp_guard, "GuardedYoutubeDL", _FakeYdl)
    monkeypatch.setattr(source_scanner, "validate_public_url", lambda u, allowed=None: u)

    with pytest.raises(source_scanner.ScanError, match="Unsupported protocol"):
        source_scanner.import_video_from_url(
            "https://www.youtube.com/watch?v=abc123", str(tmp_path))
    assert writes == []  # download never started
    assert _dest_files(str(tmp_path)) == []  # no artifacts left behind
