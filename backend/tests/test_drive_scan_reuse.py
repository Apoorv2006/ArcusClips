"""Regression tests: repeated Drive scans reuse the existing Source.

Verifies that scanning the same folder set for the same user reuses/updates
the existing Drive Source instead of creating duplicates, and keeps
video-to-source associations and discovered counts consistent.
"""
import pytest

from app.db import get_session_factory
from app.models import DriveConnection, Source, User, Video
from app.services import drive_google


@pytest.fixture()
def drive_scan_env(client, app_env, monkeypatch):
    from app.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "google_oauth_client_id", "c")
    monkeypatch.setattr(s, "google_oauth_client_secret", "x")

    videos = [
        {"id": "v1", "name": "a.mp4", "mime_type": "video/mp4", "size": 100,
         "modified_at": __import__("datetime").datetime(2026, 1, 1),
         "relative_path": "a.mp4", "target_id": "v1", "downloadable": True,
         "drive_id": ""},
        {"id": "v2", "name": "b.mp4", "mime_type": "video/mp4", "size": 200,
         "modified_at": __import__("datetime").datetime(2026, 1, 1),
         "relative_path": "b.mp4", "target_id": "v2", "downloadable": True,
         "drive_id": ""},
    ]

    monkeypatch.setattr(
        drive_google, "scan_folders",
        lambda client, folder_ids, exclude_ids=None, progress_cb=None, drive_id=None:
        (list(videos), {"total": len(videos)}),
    )
    monkeypatch.setattr(
        drive_google, "client_from_connection",
        lambda conn: (object(), {"email": "u@example.com"}),
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
        conn_id = conn.id
    finally:
        session.close()
    return conn_id


def _drive_sources(user_id):
    session = get_session_factory()()
    try:
        return session.query(Source).filter(
            Source.user_id == user_id, Source.type == "drive_folder"
        ).all()
    finally:
        session.close()


def test_repeated_scan_reuses_source(client, auth_headers, drive_scan_env):
    """Scanning the same folder set twice does not create a duplicate Source."""
    session = get_session_factory()()
    try:
        admin = session.query(User).first()
        user_id = admin.id
    finally:
        session.close()

    r1 = client.post("/api/drive/scan", headers=auth_headers,
                     json={"folder_ids": ["root", "f1"], "exclude_ids": []})
    assert r1.status_code == 200, r1.text
    sources_after_first = _drive_sources(user_id)
    assert len(sources_after_first) == 1
    source_id = sources_after_first[0].id

    r2 = client.post("/api/drive/scan", headers=auth_headers,
                     json={"folder_ids": ["root", "f1"], "exclude_ids": []})
    assert r2.status_code == 200, r2.text
    sources_after_second = _drive_sources(user_id)
    assert len(sources_after_second) == 1, "no duplicate Source created"
    assert sources_after_second[0].id == source_id, "same Source reused"


def test_repeated_scan_keeps_video_associations(client, auth_headers, drive_scan_env):
    """Videos stay associated with the reused Source; no duplicate Video rows."""
    session = get_session_factory()()
    try:
        admin = session.query(User).first()
        user_id = admin.id
    finally:
        session.close()

    client.post("/api/drive/scan", headers=auth_headers, json={"folder_ids": ["root"]})
    client.post("/api/drive/scan", headers=auth_headers, json={"folder_ids": ["root"]})

    session = get_session_factory()()
    try:
        sources = session.query(Source).filter(
            Source.user_id == user_id, Source.type == "drive_folder"
        ).all()
        assert len(sources) == 1
        source = sources[0]
        videos = session.query(Video).filter(Video.source_id == source.id).all()
        # drive_file_id dedup means each unique file has one Video row
        assert len(videos) == 2
        assert all(v.source_id == source.id for v in videos)
        assert source.discovered_count == 2
        assert source.scan_status == "ok"
    finally:
        session.close()


def test_different_folder_set_creates_new_source(client, auth_headers, drive_scan_env):
    """A different folder set creates a distinct Source (reuse is per folder set)."""
    session = get_session_factory()()
    try:
        admin = session.query(User).first()
        user_id = admin.id
    finally:
        session.close()

    client.post("/api/drive/scan", headers=auth_headers, json={"folder_ids": ["root"]})
    client.post("/api/drive/scan", headers=auth_headers, json={"folder_ids": ["other"]})
    sources = _drive_sources(user_id)
    assert len(sources) == 2
    keys = {s.url_or_id for s in sources}
    assert "root" in keys
    assert "other" in keys


def test_scan_updates_existing_source_timestamp(client, auth_headers, drive_scan_env):
    """A repeated scan updates last_scanned_at on the reused Source."""
    session = get_session_factory()()
    try:
        admin = session.query(User).first()
        user_id = admin.id
    finally:
        session.close()

    client.post("/api/drive/scan", headers=auth_headers, json={"folder_ids": ["root"]})
    first = _drive_sources(user_id)[0].last_scanned_at

    import time
    time.sleep(0.01)
    client.post("/api/drive/scan", headers=auth_headers, json={"folder_ids": ["root"]})
    second = _drive_sources(user_id)[0].last_scanned_at
    assert second is not None
    assert second >= first
