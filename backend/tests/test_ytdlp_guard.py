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
