"""Regression test: Drive scan registration must register each video exactly
once on first scan and not duplicate on a repeated scan.

Mocks the Google Drive client + scan_folders; exercises the real
/api/drive/scan route and _register_drive_videos against the DB.
"""
import pytest

from app.db import get_session_factory
from app.models import DriveConnection, Source, User, Video
from app.services import drive_google


@pytest.fixture()
def connected_drive(client, app_env, monkeypatch):
    """A connected DriveConnection with a mocked client/scan."""
    from app.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "google_oauth_client_id", "c")
    monkeypatch.setattr(s, "google_oauth_client_secret", "x")

    scanned = {"count": 0}

    def _fake_scan(client, folder_ids, exclude_ids=None, progress_cb=None, drive_id=None):
        scanned["count"] += 1
        videos = [
            {"id": "v1", "name": "a.mp4", "mime_type": "video/mp4", "size": 100,
             "modified_at": __import__("datetime").datetime(2026, 1, 1), "relative_path": "a.mp4",
             "downloadable": True},
            {"id": "v2", "name": "b.mov", "mime_type": "video/quicktime", "size": 200,
             "modified_at": __import__("datetime").datetime(2026, 1, 1), "relative_path": "b.mov",
             "downloadable": True},
        ]
        return videos, {"total": len(videos)}

    monkeypatch.setattr(drive_google, "scan_folders", _fake_scan)
    # avoid real token decryption / Google API; return a stub client
    monkeypatch.setattr(drive_google, "client_from_connection", lambda conn: (object(), {"email": "u@example.com"}))

    session = get_session_factory()()
    try:
        admin = session.query(User).filter(User.is_admin == True).first()
        user_id = admin.id
        conn = DriveConnection(
            user_id=user_id, status="connected", account_email="u@example.com",
            encrypted_access_token="enc", token_expires_at=None,
        )
        session.add(conn)
        session.flush()
        conn_id = conn.id
        session.commit()
    finally:
        session.close()
    return conn_id, scanned


def _videos_for_source(source_id):
    session = get_session_factory()()
    try:
        return session.query(Video).filter(Video.source_id == source_id).all()
    finally:
        session.close()


def test_drive_scan_registers_each_video_once(client, auth_headers, connected_drive):
    conn_id, scanned = connected_drive
    r = client.post("/api/drive/scan", headers=auth_headers,
                    json={"folder_ids": ["root"], "exclude_ids": []})
    assert r.status_code == 200, r.text
    assert r.json()["videos"] == 2

    session = get_session_factory()()
    try:
        sources = session.query(Source).filter(Source.type == "drive_folder").all()
        assert len(sources) == 1
        # source id must be set (the ordering fix)
        assert sources[0].id is not None
        videos = _videos_for_source(sources[0].id)
    finally:
        session.close()
    assert len(videos) == 2

    # ---- repeated scan: must NOT duplicate ----
    r2 = client.post("/api/drive/scan", headers=auth_headers,
                     json={"folder_ids": ["root"], "exclude_ids": []})
    assert r2.status_code == 200, r2.text
    assert r2.json()["videos"] == 2  # scan still finds 2

    session = get_session_factory()()
    try:
        # a second source is created, but no new Video rows for the same files
        all_videos = session.query(Video).all()
        # drive_file_id dedup means each unique file has one Video row
        assert len(all_videos) == 2
        # no Video row has source_id None (the old bug)
        assert all(v.source_id is not None for v in all_videos)
    finally:
        session.close()


def test_drive_scan_source_id_persisted_on_video(client, auth_headers, connected_drive):
    """Every registered Video row references its Source (no None source_id)."""
    connected_drive
    r = client.post("/api/drive/scan", headers=auth_headers,
                    json={"folder_ids": ["root"]})
    assert r.status_code == 200, r.text
    session = get_session_factory()()
    try:
        videos = session.query(Video).filter(Video.kind == "drive").all()
        assert len(videos) == 2
        for v in videos:
            assert v.source_id is not None, "Video.source_id must be set"
            src = session.get(Source, v.source_id)
            assert src is not None
            assert src.type == "drive_folder"
    finally:
        session.close()
