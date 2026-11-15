"""Regression tests: DriveFile metadata refresh and repair on rescan.

Verifies that rescanning a Drive folder updates existing DriveFile rows
(create-or-update semantics) instead of leaving stale data, that shortcut
identity is preserved while the media target is repaired, that Shared Drive
context and MD5 checksums persist, and that no duplicate Videos/Sources are
created. Also covers rows created before the target_id/drive_id migration.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.db import get_session_factory
from app.models import DriveConnection, DriveFile, Source, User, Video
from app.services import drive_google


def _meta(fid, name, parents, **extra):
    f = {"id": fid, "name": name,
         "mimeType": extra.pop("mimeType", "video/mp4"),
         "parents": parents,
         "size": extra.pop("size", "100"),
         "modifiedTime": extra.pop("modifiedTime", "2026-01-01T00:00:00.000Z"),
         "capabilities": extra.pop("capabilities", {"canDownload": True})}
    f.update(extra)
    return f


class _Req:
    def __init__(self, data=None, error=None):
        self._data = data
        self._error = error

    def execute(self):
        if self._error is not None:
            raise self._error
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
    def __init__(self, data: bytes):
        self._data = data

    def request(self, uri, method="GET", body=None, headers=None, **kwargs):
        resp = _FakeResp(
            200,
            {"content-range": f"bytes 0-{len(self._data) - 1}/{len(self._data)}"},
        )
        return resp, self._data


class _FakeMedia:
    def __init__(self, meta):
        self.uri = f"https://x/{meta.get('id')}"
        self.headers = {}
        self.http = _FakeHttp((meta.get("name", "") + "-bytes").encode())


class MutableTree:
    """A Drive tree whose entries can be mutated between scans."""

    def __init__(self, entries):
        self.entries = {e["id"]: dict(e) for e in entries}
        self.fail_get_for: set[str] = set()

    # googleapiclient-style accessors ---------------------------------------
    def files(self):
        return _Files(self)

    def drives(self):
        return _Drives()

    def about(self):
        return _Req({"user": {"email": "u@example.com"}})


class _Files:
    def __init__(self, tree: MutableTree):
        self._tree = tree

    def list(self, **params):
        assert "driveId" not in params or params.get("corpora") == "drive"
        q = params.get("q", "")
        folder_id = q.split("'")[1]
        children = [
            f for f in self._tree.entries.values()
            if folder_id in (f.get("parents") or []) and not f.get("trashed")
        ]
        return _Req({"files": children, "nextPageToken": None})

    def get(self, fileId, fields=None, **kwargs):
        assert "driveId" not in kwargs, "files.get() does not support driveId"
        if fileId in self._tree.fail_get_for:
            return _Req(error=RuntimeError("target not accessible"))
        return _Req(self._tree.entries[fileId])

    def get_media(self, fileId, **kwargs):
        assert "driveId" not in kwargs, "files.get_media() does not support driveId"
        return _FakeMedia(self._tree.entries[fileId])


class _Drives:
    def list(self, **params):
        unsupported = set(params) - {"pageSize", "pageToken", "fields", "q", "orderBy"}
        assert not unsupported, f"drives.list() does not support: {unsupported}"
        return _Req({"drives": [{"id": "shared-1", "name": "Shared Drive"}]})


@pytest.fixture()
def drive_env(client, app_env, monkeypatch):
    from app.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "google_oauth_client_id", "c")
    monkeypatch.setattr(s, "google_oauth_client_secret", "x")

    tree = MutableTree([
        _meta("root", "My Drive", [], mimeType="application/vnd.google-apps.folder"),
        _meta("f1", "Show", ["root"], mimeType="application/vnd.google-apps.folder"),
        _meta("v1", "Episode 1.mp4", ["f1"], size="100", md5Checksum="aaa", version="1"),
        _meta("vtarget", "real_episode.mp4", ["root"], size="500",
              md5Checksum="ccc", version="2"),
        _meta("link", "Episode Link", ["f1"],
              mimeType="application/vnd.google-apps.shortcut",
              shortcutDetails={"targetId": "vtarget"},
              size="0", md5Checksum="bbb"),
    ])

    monkeypatch.setattr(
        drive_google, "client_from_connection", lambda conn: (tree, {"email": "u@example.com"})
    )

    session = get_session_factory()()
    try:
        admin = session.query(User).filter(User.is_admin == True).first()  # noqa: E712
        conn = DriveConnection(
            user_id=admin.id, status="connected", account_email="u@example.com",
            encrypted_access_token="enc", token_expires_at=None,
        )
        session.add(conn)
        session.commit()
    finally:
        session.close()
    return tree


def _scan(client, auth_headers, folder_ids, **body):
    payload = {"folder_ids": folder_ids, "exclude_ids": []}
    payload.update(body)
    return client.post("/api/drive/scan", headers=auth_headers, json=payload)


def _drive_files():
    session = get_session_factory()()
    try:
        return {df.drive_file_id: df for df in session.query(DriveFile).all()}
    finally:
        session.close()


def test_rescan_normal_file_refreshes_all_metadata(client, auth_headers, drive_env):
    r = _scan(client, auth_headers, ["f1"])
    assert r.status_code == 200, r.text

    # provider metadata changes upstream
    v1 = drive_env.entries["v1"]
    v1["name"] = "Episode 1 (remastered).mp4"
    v1["size"] = "12345"
    v1["md5Checksum"] = "deadbeef"
    v1["modifiedTime"] = "2026-06-01T00:00:00.000Z"
    v1["version"] = "7"
    v1["mimeType"] = "video/x-matroska"

    r2 = _scan(client, auth_headers, ["f1"])
    assert r2.status_code == 200, r2.text

    files = _drive_files()
    df = files["v1"]
    assert df.name == "Episode 1 (remastered).mp4"
    assert df.size == 12345
    assert df.checksum == "deadbeef"
    assert df.version == "7"
    assert df.mime_type == "video/x-matroska"
    assert df.is_shortcut is False
    assert df.target_id == "v1"  # normal file: target == own id

    # no duplicates: exactly one DriveFile and one Video for v1
    session = get_session_factory()()
    try:
        assert session.query(DriveFile).filter(DriveFile.drive_file_id == "v1").count() == 1
        df_row = session.query(DriveFile).filter(DriveFile.drive_file_id == "v1").first()
        videos = session.query(Video).filter(Video.drive_file_id == df_row.id).all()
        assert len(videos) == 1
        assert videos[0].name == "Episode 1 (remastered).mp4"
    finally:
        session.close()


def test_rescan_shortcut_preserves_identity_and_refreshes(client, auth_headers, drive_env):
    r = _scan(client, auth_headers, ["f1"])
    assert r.status_code == 200, r.text

    # upstream: shortcut renamed + target changes size/checksum
    drive_env.entries["link"]["name"] = "Episode Link (new)"
    drive_env.entries["vtarget"]["size"] = "999"
    drive_env.entries["vtarget"]["md5Checksum"] = "feedface"

    r2 = _scan(client, auth_headers, ["f1"])
    assert r2.status_code == 200, r2.text

    files = _drive_files()
    df = files["link"]
    assert df.drive_file_id == "link"  # shortcut ID preserved as identity
    assert df.target_id == "vtarget"  # media target is the resolved file
    assert df.name == "Episode Link (new)"
    assert df.is_shortcut is True

    session = get_session_factory()()
    try:
        vids = session.query(Video).filter(Video.drive_file_id == df.id).all()
        assert len(vids) == 1
        assert vids[0].name == "Episode Link (new)"
    finally:
        session.close()


def test_pre_migration_normal_file_row_is_repaired(client, auth_headers, drive_env, app_env):
    """Rows written before the target_id/drive_id migration (empty target_id)
    are repaired on the next rescan."""
    session = get_session_factory()()
    try:
        admin = session.query(User).filter(User.is_admin == True).first()  # noqa: E712
        conn = session.query(DriveConnection).filter(DriveConnection.user_id == admin.id).first()
        df = DriveFile(
            user_id=admin.id, connection_id=conn.id, drive_file_id="v1",
            target_id="", drive_id="",  # pre-migration shape
            name="stale.mp4", mime_type="video/mp4", size=1,
            modified_at=datetime(2025, 1, 1), parent_id="f1",
            relative_path="stale.mp4", is_shortcut=False, downloadable=True,
            checksum="", version="", import_status="imported",
        )
        session.add(df)
        session.commit()
        df_id = df.id
    finally:
        session.close()

    r = _scan(client, auth_headers, ["f1"])
    assert r.status_code == 200, r.text

    files = _drive_files()
    df = files["v1"]
    assert df.id == df_id  # same row, repaired in place
    assert df.target_id == "v1"
    assert df.checksum == "aaa"
    assert df.version == "1"
    assert df.name == "Episode 1.mp4"
    # no duplicate Videos/DriveFiles created by the repair
    session = get_session_factory()()
    try:
        assert session.query(Video).filter(Video.drive_file_id == df_id).count() == 1
    finally:
        session.close()


def test_pre_migration_shortcut_row_target_is_repaired(client, auth_headers, drive_env):
    """A pre-migration shortcut row with an empty target_id is repaired from
    provider metadata — the shortcut's own ID is never assumed to be the
    target."""
    session = get_session_factory()()
    try:
        admin = session.query(User).filter(User.is_admin == True).first()  # noqa: E712
        conn = session.query(DriveConnection).filter(DriveConnection.user_id == admin.id).first()
        df = DriveFile(
            user_id=admin.id, connection_id=conn.id, drive_file_id="link",
            target_id="", drive_id="",
            name="Episode Link", mime_type="application/vnd.google-apps.shortcut",
            size=0, modified_at=datetime(2025, 1, 1), parent_id="f1",
            relative_path="Episode Link", is_shortcut=True, downloadable=True,
            checksum="", version="", import_status="imported",
        )
        session.add(df)
        session.commit()
    finally:
        session.close()

    r = _scan(client, auth_headers, ["f1"])
    assert r.status_code == 200, r.text

    files = _drive_files()
    df = files["link"]
    assert df.target_id == "vtarget"  # resolved from provider metadata, not "link"
    assert df.is_shortcut is True
    # checksum is the resolved target's md5 (used to validate media bytes)
    assert df.checksum == "ccc"


def test_unresolved_shortcut_does_not_wipe_known_target(client, auth_headers, drive_env):
    r = _scan(client, auth_headers, ["f1"])
    assert r.status_code == 200, r.text
    files = _drive_files()
    assert files["link"].target_id == "vtarget"

    # target becomes inaccessible upstream; shortcut still listed
    drive_env.fail_get_for.add("vtarget")
    r2 = _scan(client, auth_headers, ["f1"])
    assert r2.status_code == 200, r2.text
    files = _drive_files()
    assert files["link"].target_id == "vtarget"  # not wiped


def test_rescan_persists_shared_drive_context_and_checksum(client, auth_headers, drive_env):
    r = _scan(client, auth_headers, ["f1"], drive_id="shared-1")
    assert r.status_code == 200, r.text

    files = _drive_files()
    assert files["v1"].drive_id == "shared-1"
    assert files["v1"].checksum == "aaa"
    assert files["link"].drive_id == "shared-1"

    # and the source recorded the drive context for future scans
    session = get_session_factory()()
    try:
        source = session.query(Source).filter(Source.type == "drive_folder").first()
        assert (source.config or {}).get("drive_id") == "shared-1"
    finally:
        session.close()


def test_checksum_is_used_for_download_integrity(client, auth_headers, drive_env, tmp_path, monkeypatch):
    r = _scan(client, auth_headers, ["f1"])
    assert r.status_code == 200, r.text

    captured = {}

    def _fake_download(client_arg, file_id, out_path, **kwargs):
        captured["file_id"] = file_id
        captured["checksum"] = kwargs.get("checksum")
        captured["file_size"] = kwargs.get("file_size")
        captured["cancel_event"] = kwargs.get("cancel_event")
        with open(out_path, "wb") as fh:
            fh.write(b"payload")
        return str(out_path)

    monkeypatch.setattr(drive_google, "download_file", _fake_download)

    from app.services import jobs

    session = get_session_factory()()
    try:
        admin = session.query(User).filter(User.is_admin == True).first()  # noqa: E712
        df = session.query(DriveFile).filter(DriveFile.drive_file_id == "link").first()
        video = session.query(Video).filter(Video.drive_file_id == df.id).first()
        video.local_path = ""  # force the download path
        job_cancel = threading_event = __import__("threading").Event()
        jobs.ensure_video_imported(
            session, admin.id, video, job=None, cancel_event=job_cancel,
        )
        assert captured["file_id"] == "vtarget"  # shortcut target used, not "link"
        assert captured["checksum"] == "ccc"  # the target's md5 validates the media bytes
        assert captured["file_size"] == 500  # the target's size
        assert captured["cancel_event"] is not None
    finally:
        session.close()


def test_drive_scan_api_threads_drive_id_into_scan(client, auth_headers, drive_env, monkeypatch):
    """/api/drive/scan passes drive_id (driveId) through to the scan."""
    captured = {}

    def _fake_scan(client_arg, folder_ids, exclude_ids=None, progress_cb=None, drive_id=None):
        captured["drive_id"] = drive_id
        return [], {"visited": 0, "files": 0, "folders": 0, "errors": 0, "skipped": 0}

    monkeypatch.setattr(drive_google, "scan_folders", _fake_scan)
    r = _scan(client, auth_headers, ["sd-root"], drive_id="shared-1")
    assert r.status_code == 200, r.text
    assert captured["drive_id"] == "shared-1"


def test_drive_folder_endpoint_lists_with_drive_id(client, auth_headers, drive_env, monkeypatch):
    captured = {}

    real_list_children = drive_google.list_children

    def _spy(client_arg, folder_id, drive_id=None):
        captured["drive_id"] = drive_id
        return real_list_children(client_arg, folder_id, drive_id=drive_id)

    monkeypatch.setattr(drive_google, "list_children", _spy)
    r = client.get("/api/drive/folders/f1?drive_id=shared-1", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert captured["drive_id"] == "shared-1"


def test_drive_cancel_normalises_to_download_cancelled(client, auth_headers, drive_env, tmp_path, monkeypatch):
    """A Drive download cancelled mid-flight must surface as the shared
    DownloadCancelled signal the worker/jobs layer understands — not as a
    generic (failed) error."""
    r = _scan(client, auth_headers, ["f1"])
    assert r.status_code == 200, r.text

    def _fake_download(client_arg, file_id, out_path, **kwargs):
        raise drive_google.DriveCancelled("Download cancelled")

    monkeypatch.setattr(drive_google, "download_file", _fake_download)

    import threading

    from app.services import jobs, source_scanner

    session = get_session_factory()()
    try:
        admin = session.query(User).filter(User.is_admin == True).first()  # noqa: E712
        df = session.query(DriveFile).filter(DriveFile.drive_file_id == "v1").first()
        video = session.query(Video).filter(Video.drive_file_id == df.id).first()
        video.local_path = ""
        with pytest.raises(source_scanner.DownloadCancelled):
            jobs.ensure_video_imported(
                session, admin.id, video, job=None, cancel_event=threading.Event(),
            )
    finally:
        session.close()


def test_url_import_cancel_propagates(client, auth_headers, app_env, monkeypatch, tmp_path):
    """The URL import path propagates the shared DownloadCancelled signal."""
    import threading

    from app.services import jobs, source_scanner

    def _boom(url, dest_dir, progress_cb=None, cancel_event=None):
        raise source_scanner.DownloadCancelled("Download cancelled")

    monkeypatch.setattr(source_scanner, "import_video_from_url", _boom)

    session = get_session_factory()()
    try:
        admin = session.query(User).filter(User.is_admin == True).first()  # noqa: E712
        video = Video(
            user_id=admin.id, kind="url", name="clip",
            origin_url="https://example.com/v.mp4", status="discovered",
        )
        session.add(video)
        session.commit()
        with pytest.raises(source_scanner.DownloadCancelled):
            jobs.ensure_video_imported(
                session, admin.id, video, job=None, cancel_event=threading.Event(),
            )
    finally:
        session.close()


def test_rescan_refreshes_changed_shared_drive_context(client, auth_headers, drive_env):
    """Rescanning with a different drive_id updates the stored Shared Drive
    context on existing rows (and reuses the source for the original context)."""
    r = _scan(client, auth_headers, ["f1"])
    assert r.status_code == 200, r.text
    files = _drive_files()
    assert files["v1"].drive_id == ""
    assert files["link"].drive_id == ""

    r2 = _scan(client, auth_headers, ["f1"], drive_id="shared-1")
    assert r2.status_code == 200, r2.text
    files = _drive_files()
    assert files["v1"].drive_id == "shared-1"
    assert files["link"].drive_id == "shared-1"

    # rescanning back to My Drive restores the empty context
    r3 = _scan(client, auth_headers, ["f1"], drive_id=None)
    assert r3.status_code == 200, r3.text
    files = _drive_files()
    assert files["v1"].drive_id == ""
    assert files["link"].drive_id == ""


def _md5_hex(data: bytes) -> str:
    import hashlib

    return hashlib.md5(data).hexdigest()


def test_scanned_checksum_validates_download_end_to_end(client, auth_headers, drive_env, tmp_path, monkeypatch):
    """The md5 persisted at scan time is used to validate the bytes the
    download writes — end to end through ensure_video_imported."""
    import threading

    # the fake serves (name + "-bytes") as the payload
    payload = b"Episode 1.mp4-bytes"
    drive_env.entries["v1"]["md5Checksum"] = _md5_hex(payload)
    drive_env.entries["v1"]["size"] = str(len(payload))

    r = _scan(client, auth_headers, ["f1"])
    assert r.status_code == 200, r.text

    monkeypatch.setattr(drive_google.time, "sleep", lambda s: None)

    from app.services import jobs

    session = get_session_factory()()
    try:
        admin = session.query(User).filter(User.is_admin == True).first()  # noqa: E712
        df = session.query(DriveFile).filter(DriveFile.drive_file_id == "v1").first()
        assert df.checksum == _md5_hex(payload)
        video = session.query(Video).filter(Video.drive_file_id == df.id).first()
        video.local_path = ""
        jobs.ensure_video_imported(session, admin.id, video, job=None,
                                   cancel_event=threading.Event())
        from pathlib import Path

        assert Path(video.local_path).exists()
    finally:
        session.close()


def test_download_with_stale_checksum_fails_and_leaves_nothing(client, auth_headers, drive_env, tmp_path, monkeypatch):
    """A checksum that no longer matches the provider bytes fails the import
    (integrity enforcement), and no file is left behind."""
    import threading

    drive_env.entries["v1"]["md5Checksum"] = "0" * 32  # stale/wrong md5
    drive_env.entries["v1"]["size"] = str(len(b"Episode 1.mp4-bytes"))

    r = _scan(client, auth_headers, ["f1"])
    assert r.status_code == 200, r.text

    monkeypatch.setattr(drive_google.time, "sleep", lambda s: None)

    from app.services import jobs

    session = get_session_factory()()
    try:
        admin = session.query(User).filter(User.is_admin == True).first()  # noqa: E712
        df = session.query(DriveFile).filter(DriveFile.drive_file_id == "v1").first()
        video = session.query(Video).filter(Video.drive_file_id == df.id).first()
        video.local_path = ""
        with pytest.raises(drive_google.DriveError) as exc:
            jobs.ensure_video_imported(session, admin.id, video, job=None,
                                       cancel_event=threading.Event())
        assert "Checksum mismatch" in str(exc.value)
        # no final file produced
        assert not video.local_path or not __import__("pathlib").Path(video.local_path or "").exists()
    finally:
        session.close()
