"""API-level tests: auth, settings, sources, videos, cross-user isolation."""
import pytest


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_login_and_me(client, auth_headers):
    r = client.get("/api/auth/me", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["username"] == "admin"


def test_login_rejects_bad_credentials(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "nope"})
    assert r.status_code == 401


def test_protected_routes_require_token(client):
    assert client.get("/api/dashboard").status_code == 401
    assert client.get("/api/settings").status_code == 401


def test_settings_get_update_roundtrip(client, auth_headers):
    r = client.get("/api/settings", headers=auth_headers)
    assert r.status_code == 200
    r = client.put("/api/settings", headers=auth_headers, json={
        "lmstudio": {"base_url": "http://127.0.0.1:1234/v1", "model": "local-model",
                     "api_key": "sk-test", "request_timeout": 90, "max_tokens": 1024,
                     "temperature": 0.3, "max_context_tokens": 4096},
        "branding": {"creator_handle": "@creator", "default_caption_style": "hype",
                     "default_layout": "zoom", "font_name": "DejaVu Sans"},
    })
    assert r.status_code == 200
    r = client.get("/api/settings", headers=auth_headers)
    body = r.json()
    assert body["lmstudio"]["model"] == "local-model"
    assert body["lmstudio"]["api_key"] == ""  # never returned
    assert body["branding"]["creator_handle"] == "@creator"


def test_lmstudio_test_endpoint_reports_unavailable(client, auth_headers):
    r = client.post("/api/settings/lmstudio/test", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["connected"] is False  # nothing on 127.0.0.1:1234 in tests


def test_sources_crud(client, auth_headers):
    r = client.post("/api/sources", headers=auth_headers, json={
        "type": "webpage", "name": "Article", "url_or_id": "https://example.com/article"})
    assert r.status_code == 200
    source = r.json()
    assert source["type"] == "webpage"
    r = client.get("/api/sources", headers=auth_headers)
    assert any(s["id"] == source["id"] for s in r.json())
    r = client.post(f"/api/sources/{source['id']}/monitor", params={"monitoring": False},
                    headers=auth_headers)
    assert r.json()["monitoring"] is False
    r = client.delete(f"/api/sources/{source['id']}", headers=auth_headers)
    assert r.json()["ok"]


def test_source_validation(client, auth_headers):
    r = client.post("/api/sources", headers=auth_headers, json={
        "type": "bogus", "name": "x", "url_or_id": "y"})
    assert r.status_code == 400


def test_drive_status_requires_connection(client, auth_headers):
    r = client.get("/api/drive/status", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["connected"] is False


def test_video_upload_and_validation(client, auth_headers, sample_video):
    import shutil
    from pathlib import Path

    bad = Path(sample_video).with_name("bad.txt")
    bad.write_text("not a video")
    with open(bad, "rb") as fh:
        r = client.post("/api/videos/upload", files={"file": ("bad.txt", fh, "text/plain")},
                        headers=auth_headers)
    assert r.status_code == 400
    bad.unlink()

    good = Path(sample_video).with_name("good.mp4")
    shutil.copy(sample_video, good)
    with open(good, "rb") as fh:
        r = client.post("/api/videos/upload", files={"file": ("good.mp4", fh, "video/mp4")},
                        headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["media_valid"] is True
    assert body["duration"] > 0
    assert body["status"] == "imported"


def test_transcript_missing_before_processing(client, auth_headers, uploaded_video):
    r = client.get(f"/api/videos/{uploaded_video['id']}/transcript", headers=auth_headers)
    assert r.status_code == 404


def test_batch_create_and_lifecycle(client, auth_headers, uploaded_video):
    r = client.post("/api/batches", headers=auth_headers, json={
        "name": "Test Batch", "video_ids": [uploaded_video["id"]],
        "clips_per_video": 2, "duration": {"min_seconds": 15, "max_seconds": 30},
        "quality": "balanced", "caption_style": "bounce", "layout": "blur",
    })
    assert r.status_code == 200
    batch = r.json()
    assert batch["total_videos"] == 1
    r = client.get(f"/api/batches/{batch['id']}/jobs", headers=auth_headers)
    assert len(r.json()) == 1
    r = client.get(f"/api/batches/{batch['id']}", headers=auth_headers)
    assert r.status_code == 200
    # pause/resume/cancel on a fresh batch
    r = client.post(f"/api/batches/{batch['id']}/pause", headers=auth_headers)
    assert r.json()["ok"]
    r = client.post(f"/api/batches/{batch['id']}/resume", headers=auth_headers)
    assert r.json()["ok"]
    r = client.post(f"/api/batches/{batch['id']}/cancel", headers=auth_headers)
    assert r.json()["ok"]
    r = client.get(f"/api/batches/{batch['id']}", headers=auth_headers)
    assert r.json()["status"] == "cancelled"


def test_clips_empty(client, auth_headers):
    r = client.get("/api/clips", headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == []


def test_exports_empty(client, auth_headers):
    r = client.get("/api/exports", headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == []


def test_cross_user_isolation(app_env, auth_headers, client):
    # create a second user and verify isolation
    from app.auth import create_user
    from app.db import get_session_factory

    session = get_session_factory()()
    try:
        create_user(session, "other", "pw12345")
        session.commit()
    finally:
        session.close()
    r = client.post("/api/auth/login", json={"username": "other", "password": "pw12345"})
    assert r.status_code == 200
    other_headers = {"Authorization": f"Bearer {r.json()['token']}"}
    # other user sees no sources/batches of admin
    assert client.get("/api/sources", headers=other_headers).json() == []
    assert client.get("/api/batches", headers=other_headers).json() == []
    # admin's sources not visible to other
    r = client.post("/api/sources", headers=auth_headers, json={
        "type": "video_url", "name": "Admin Source", "url_or_id": "https://cdn.example.com/a.mp4"})
    admin_source_id = r.json()["id"]
    r = client.get(f"/api/sources/{admin_source_id}", headers=other_headers)
    assert r.status_code in (404, 405)
