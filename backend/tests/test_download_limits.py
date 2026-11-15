"""Regression tests: download size limits and cancellation during streaming.

Verifies ARCUS_MAX_DOWNLOAD_SIZE is enforced while streaming even when the
provider does not report the expected size, and that downloads stop immediately
on cancellation or when the size limit is exceeded.
"""
import threading

import pytest

from app.services import drive_google


class _Req:
    def __init__(self, data=None):
        self._data = data or {}

    def execute(self):
        return self._data


class _FakeResp:
    def __init__(self, status, headers):
        self.status = status
        self._headers = headers

    def __contains__(self, key):
        return key in self._headers

    def __getitem__(self, key):
        return self._headers[key]


class _FakeHttp:
    """Serves a fixed payload; MediaIoBaseDownload writes it to the file."""

    def __init__(self, data):
        self._data = data

    def request(self, uri, method="GET", body=None, headers=None, **kwargs):
        resp = _FakeResp(200, {"content-range": f"bytes 0-{len(self._data) - 1}/{len(self._data)}"})
        return resp, self._data


class _FakeMedia:
    def __init__(self, data: bytes):
        self.uri = "https://x/media"
        self.headers = {}
        self.http = _FakeHttp(data)


class _FakeFiles:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.media_calls = []

    def get_media(self, fileId, **kwargs):
        self.media_calls.append({"fileId": fileId, **kwargs})
        return _FakeMedia(self.payload)


class _FakeDrive:
    def __init__(self, payload: bytes):
        self._files = _FakeFiles(payload)

    def files(self):
        return self._files

    def drives(self):
        return _Req({"drives": []})

    def about(self):
        return _Req({"user": {"email": "u@example.com"}})


def test_streaming_size_limit_enforced_when_size_unknown(tmp_path, app_env):
    """When file_size is 0 (unknown), the streaming limit still stops the download."""
    import app.config
    old = app.config.settings.max_download_size
    app.config.settings.max_download_size = 10
    try:
        # payload far larger than the limit
        client = _FakeDrive(b"x" * 10_000)
        out = tmp_path / "big.mp4"
        with pytest.raises(drive_google.DriveError) as exc:
            drive_google.download_file(client, "f1", out, file_size=0)
        assert "max size" in str(exc.value).lower()
        # no final file written (only a .part, which is cleaned up on failure)
        assert not out.exists()
        assert not (tmp_path / "big.mp4.part").exists()
    finally:
        app.config.settings.max_download_size = old


def test_cancel_stops_download_immediately(tmp_path, app_env):
    """A set cancel_event stops the download before completion."""
    client = _FakeDrive(b"x" * 1000)
    out = tmp_path / "c.mp4"
    cancel = threading.Event()
    cancel.set()  # already cancelled
    with pytest.raises(drive_google.DriveError) as exc:
        drive_google.download_file(client, "f1", out, file_size=0, cancel_event=cancel)
    assert "cancelled" in str(exc.value).lower()
    assert not out.exists()


def test_download_succeeds_under_limit(tmp_path, app_env):
    """A payload under the limit downloads and validates successfully."""
    import app.config
    old = app.config.settings.max_download_size
    app.config.settings.max_download_size = 100_000
    try:
        client = _FakeDrive(b"hello world")
        out = tmp_path / "ok.mp4"
        path = drive_google.download_file(client, "f1", out, file_size=0)
        assert path == str(out)
        assert out.read_bytes() == b"hello world"
    finally:
        app.config.settings.max_download_size = old


def test_pre_check_rejects_oversized_known_size(tmp_path, app_env):
    """When file_size is known and exceeds the limit, reject before downloading."""
    import app.config
    old = app.config.settings.max_download_size
    app.config.settings.max_download_size = 10
    try:
        client = _FakeDrive(b"x" * 1000)
        with pytest.raises(drive_google.DriveError) as exc:
            drive_google.download_file(client, "f1", tmp_path / "x.mp4", file_size=1000)
        assert "max download size" in str(exc.value).lower()
    finally:
        app.config.settings.max_download_size = old
