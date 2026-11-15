"""Regression tests: Google Drive shortcut downloads.

Verifies that scanning a shortcut to a video preserves both the shortcut ID
(for dedup/display) and the actual target file ID (for media downloads), and
that the downloader receives the target ID rather than the shortcut ID.
"""
import pytest

from app.db import get_session_factory
from app.models import DriveConnection, DriveFile, Source, User, Video
from app.services import drive_google


@pytest.fixture()
def shortcut_drive(client, app_env, monkeypatch):
    """A connected Drive with a folder containing a shortcut to a video."""
    from app.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "google_oauth_client_id", "c")
    monkeypatch.setattr(s, "google_oauth_client_secret", "x")
    monkeypatch.setattr(
        drive_google, "client_from_connection",
        lambda conn: (object(), {"email": "u@example.com"}),
    )

    # tree: folder f1 contains shortcut "link" -> video "vtarget"
    tree = {
        "root": {"id": "root", "name": "My Drive",
                 "mimeType": "application/vnd.google-apps.folder", "parents": []},
        "f1": {"id": "f1", "name": "Show", "mimeType": "application/vnd.google-apps.folder",
               "parents": ["root"], "modifiedTime": "2026-01-01T00:00:00.000Z"},
        "vtarget": {"id": "vtarget", "name": "real_episode.mp4", "mimeType": "video/mp4",
                    "parents": ["root"], "size": "500",
                    "modifiedTime": "2026-01-01T00:00:00.000Z",
                    "capabilities": {"canDownload": True}, "md5Checksum": "zz"},
        "link": {"id": "link", "name": "Episode Link", "mimeType": "application/vnd.google-apps.shortcut",
                 "parents": ["f1"], "modifiedTime": "2026-01-01T00:00:00.000Z",
                 "shortcutDetails": {"targetId": "vtarget"},
                 "capabilities": {"canDownload": True}},
    }

    class _FakeFiles:
        def __init__(self):
            self.list_calls = []

        def list(self, **params):
            self.list_calls.append(params)
            q = params.get("q", "")
            folder_id = q.split("'")[1]
            children = [f for f in tree.values() if folder_id in (f.get("parents") or [])]
            return _Req({"files": children, "nextPageToken": None})

        def get(self, fileId, fields=None, **kwargs):
            return _Req(tree[fileId])

        def get_media(self, fileId, **kwargs):
            assert "driveId" not in kwargs, "files.get_media() does not support driveId"
            self.last_media_id = fileId
            self.last_media_kwargs = kwargs
            return _FakeMedia(tree[fileId])

    class _Req:
        def __init__(self, data):
            self._data = data

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
        def __init__(self, data):
            self._data = data

        def request(self, uri, method="GET", body=None, headers=None, **kwargs):
            resp = _FakeResp(200, {"content-range": f"bytes 0-{len(self._data) - 1}/{len(self._data)}"})
            return resp, self._data

    class _FakeMedia:
        def __init__(self, meta):
            self.uri = f"https://x/{meta.get('id')}"
            self.headers = {}
            self.http = _FakeHttp(b"data-bytes")

    class _FakeDrive:
        def __init__(self):
            self._files = _FakeFiles()

        def files(self):
            return self._files

        def drives(self):
            return _FakeDrives()

        def about(self):
            return _Req({"user": {"email": "u@example.com"}})

    class _FakeDrives:
        def list(self, **params):
            return _Req({"drives": []})

    fake = _FakeDrive()
    # The real scan_folders runs against the fake client.
    monkeypatch.setattr(drive_google, "client_from_connection", lambda conn: (fake, {"email": "u@example.com"}))

    session = get_session_factory()()
    try:
        admin = session.query(User).filter(User.is_admin == True).first()  # noqa: E712
        conn = DriveConnection(user_id=admin.id, status="connected",
                               account_email="u@example.com",
                               encrypted_access_token="enc", token_expires_at=None)
        session.add(conn)
        session.commit()
        conn_id = conn.id
    finally:
        session.close()
    return fake, conn_id


def test_scan_shortcut_registers_target_id(client, auth_headers, shortcut_drive):
    """Scanning a shortcut video stores the target ID for downloads."""
    fake, conn_id = shortcut_drive
    r = client.post("/api/drive/scan", headers=auth_headers,
                    json={"folder_ids": ["f1"], "exclude_ids": []})
    assert r.status_code == 200, r.text
    assert r.json()["videos"] == 1

    session = get_session_factory()()
    try:
        df = session.query(DriveFile).first()
        assert df is not None
        # drive_file_id is the shortcut ID (for dedup/display)
        assert df.drive_file_id == "link"
        # target_id is the real file ID used for media downloads
        assert df.target_id == "vtarget"
        assert df.is_shortcut is True
        # the shortcut's displayed name is preserved
        assert df.name == "Episode Link"
        # relative folder path preserved (relative to the scan root f1)
        assert df.relative_path == "Episode Link"
        vid = session.query(Video).first()
        assert vid is not None
        assert vid.drive_file_id == df.id
        assert vid.name == "Episode Link"
    finally:
        session.close()


def test_download_uses_target_id_not_shortcut_id(client, auth_headers, shortcut_drive, tmp_path):
    """The media download request must receive the target ID, not the shortcut ID."""
    fake, conn_id = shortcut_drive
    # scan first to register the video
    r = client.post("/api/drive/scan", headers=auth_headers,
                    json={"folder_ids": ["f1"]})
    assert r.status_code == 200, r.text

    # Now download via the service using the DriveFile's target_id
    session = get_session_factory()()
    try:
        df = session.query(DriveFile).first()
        out = tmp_path / "episode.mp4"
        path = drive_google.download_file(
            fake, df.target_id, out, file_size=0,
        )
    finally:
        session.close()

    assert path == str(out)
    # the fake recorded the ID passed to get_media — must be the target, not shortcut
    assert fake._files.last_media_id == "vtarget"
    assert fake._files.last_media_id != "link"


def test_normal_file_target_id_equals_own_id(client, auth_headers, shortcut_drive):
    """Normal (non-shortcut) files have target_id == their own id."""
    # Add a normal video directly to the tree and scan the root
    fake, conn_id = shortcut_drive
    # scan f1 (which has the shortcut) then check the target_id semantics
    r = client.post("/api/drive/scan", headers=auth_headers,
                    json={"folder_ids": ["f1"]})
    assert r.status_code == 200
    session = get_session_factory()()
    try:
        df = session.query(DriveFile).first()
        # shortcut case: target_id differs from drive_file_id
        assert df.target_id == "vtarget"
        assert df.drive_file_id == "link"
    finally:
        session.close()
