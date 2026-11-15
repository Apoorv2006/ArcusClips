"""End-to-end test: full batch pipeline from upload to ZIP export.

Transcription is stubbed with deterministic fake segments because faster-whisper
model weights are downloaded from huggingface.co (not reachable in CI); the
rendering, job queue, selection and export stages run for real.
"""
import time
import zipfile
from pathlib import Path

import pytest

from app.db import get_session_factory

FAKE_SEGMENTS = [
    {"start": 0.0, "end": 4.0, "text": "Today we are going to reveal something amazing.",
     "words": [{"word": "Today", "start": 0.0, "end": 1.0},
               {"word": "we", "start": 1.0, "end": 2.0}], "word_count": 2},
    {"start": 4.0, "end": 9.0, "text": "Why is this important for you to understand?",
     "words": [], "word_count": 0},
    {"start": 9.0, "end": 14.0, "text": "The truth is most people never figure this out.",
     "words": [], "word_count": 0},
    {"start": 14.0, "end": 20.0, "text": "Here is the exact process you need to follow.",
     "words": [], "word_count": 0},
    {"start": 20.0, "end": 26.0, "text": "I think this will change how you work entirely.",
     "words": [], "word_count": 0},
    {"start": 26.0, "end": 32.0, "text": "Let me show you an incredible example right now.",
     "words": [], "word_count": 0},
    {"start": 32.0, "end": 38.0, "text": "This technique is proven and works every time.",
     "words": [], "word_count": 0},
    {"start": 38.0, "end": 44.0, "text": "The most surprising result happens at the end.",
     "words": [], "word_count": 0},
    {"start": 44.0, "end": 50.0, "text": "Keep watching for the bonus steps ahead.",
     "words": [], "word_count": 0},
    {"start": 50.0, "end": 56.0, "text": "You will love this final takeaway for sure.",
     "words": [], "word_count": 0},
]


@pytest.fixture()
def fast_render(app_env):
    """Faster render settings so E2E tests finish quickly."""
    app_env.render_preset = "ultrafast"
    app_env.render_crf = 28
    return app_env


@pytest.fixture()
def stub_transcription(monkeypatch, fast_render):
    from app.services import whisper_service

    def _fake_transcribe(video_path, wav_path, progress_cb=None, language=None):
        if progress_cb:
            progress_cb(1.0, "stub transcription complete")
        return {"language": "en", "duration": 60.0, "segments": FAKE_SEGMENTS, "model": "stub"}

    monkeypatch.setattr(whisper_service, "transcribe_video_file", _fake_transcribe)
    return _fake_transcribe


def _wait_for_batch(client, headers, batch_id, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/batches/{batch_id}", headers=headers)
        batch = r.json()
        if batch["status"] in ("completed", "partial", "failed", "cancelled"):
            return batch
        time.sleep(2)
    raise AssertionError(f"Batch {batch_id} did not finish in {timeout}s (status={batch['status']})")


def test_full_batch_pipeline_and_zip(client, auth_headers, uploaded_video, stub_transcription):
    # 1. create the batch
    r = client.post("/api/batches", headers=auth_headers, json={
        "name": "E2E Batch", "video_ids": [uploaded_video["id"]],
        "clips_per_video": 3, "duration": {"min_seconds": 8, "max_seconds": 12},
        "quality": "balanced", "caption_style": "bounce", "layout": "blur",
        "creator_handle": "@arcus",
    })
    assert r.status_code == 200
    batch = r.json()
    batch_id = batch["id"]

    # 2. let the worker run the pipeline
    batch = _wait_for_batch(client, auth_headers, batch_id)
    assert batch["status"] == "completed", batch
    assert batch["completed_videos"] == 1
    assert batch["clip_count"] == 3

    # 3. clips exist with rendered versions
    r = client.get(f"/api/clips?batch_id={batch_id}", headers=auth_headers)
    clips = r.json()
    assert len(clips) == 3
    for clip in clips:
        assert clip["status"] == "rendered"
        assert clip["versions"], "clip must have a rendered version"
        ver = clip["versions"][-1]
        assert ver["status"] == "rendered"
        assert Path(ver["render_path"]).exists()
        assert ver["duration"] > 0
        assert clip["hook"]
        assert clip["score"] > 0

    # 4. individual download works
    r = client.get(f"/api/clips/{clips[0]['id']}/download", headers=auth_headers)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("video/")
    assert len(r.content) > 1000

    # 5. ZIP export
    r = client.post(f"/api/batches/{batch_id}/export", headers=auth_headers)
    assert r.status_code == 200, r.text
    export = r.json()
    assert export["status"] == "done"
    assert Path(export["zip_path"]).exists()

    with zipfile.ZipFile(export["zip_path"]) as zf:
        names = zf.namelist()
        assert any(n.startswith("clips/") and n.endswith(".mp4") for n in names)
        assert "metadata/manifest.csv" in names
        assert "metadata/manifest.json" in names
        assert "metadata/batch_summary.json" in names
        assert any(n.startswith("thumbnails/") for n in names)
        manifest = zf.read("metadata/manifest.json").decode()
        assert "full_episode" in manifest or "source" in manifest or "hook" in manifest
        csv_text = zf.read("metadata/manifest.csv").decode()
        assert "selection_score" in csv_text

    # 6. export download endpoint
    r = client.get(f"/api/exports/{export['id']}/download", headers=auth_headers)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"


def test_clip_edit_creates_new_version(client, auth_headers, uploaded_video, stub_transcription):
    r = client.post("/api/batches", headers=auth_headers, json={
        "name": "Edit Batch", "video_ids": [uploaded_video["id"]],
        "clips_per_video": 1, "duration": {"min_seconds": 8, "max_seconds": 12},
    })
    batch_id = r.json()["id"]
    batch = _wait_for_batch(client, auth_headers, batch_id)
    assert batch["status"] == "completed"

    r = client.get(f"/api/clips?batch_id={batch_id}", headers=auth_headers)
    clip = r.json()[0]
    old_versions = len(clip["versions"])
    assert old_versions == 1

    # edit the clip metadata + timestamps, then re-render
    r = client.put(f"/api/clips/{clip['id']}", headers=auth_headers, json={
        "hook": "New hook text", "caption_style": "hype", "layout": "zoom",
        "start_time": clip["start_time"] + 1, "end_time": clip["end_time"] - 1,
    })
    assert r.status_code == 200
    assert r.json()["hook"] == "New hook text"
    assert r.json()["caption_style"] == "hype"
    assert r.json()["status"] == "selected"

    r = client.post(f"/api/clips/{clip['id']}/render", headers=auth_headers)
    assert r.status_code == 200, r.text

    r = client.get(f"/api/clips/{clip['id']}", headers=auth_headers)
    updated = r.json()
    assert len(updated["versions"]) == old_versions + 1
    assert updated["current_version"] == 2
    # previous version preserved
    assert updated["versions"][0]["version"] == 1
    assert updated["versions"][1]["version"] == 2
    assert updated["versions"][1]["status"] == "rendered"


def test_batch_failure_isolated(client, auth_headers, stub_transcription, uploaded_video, sample_video):
    """A broken video in the batch fails its own job; the batch continues."""
    from app.db import get_session_factory
    from app.models import Video

    # create a bogus video row pointing at a nonexistent file
    session = get_session_factory()()
    try:
        bad = Video(user_id=1, kind="upload", name="broken.mp4",
                    local_path="/nonexistent/broken.mp4", status="imported",
                    media_valid=False)
        session.add(bad)
        session.flush()
        session.commit()
        bad_id = bad.id
    finally:
        session.close()

    r = client.post("/api/batches", headers=auth_headers, json={
        "name": "Mixed Batch", "video_ids": [bad_id, uploaded_video["id"]],
        "clips_per_video": 1, "duration": {"min_seconds": 8, "max_seconds": 12},
    })
    batch_id = r.json()["id"]
    batch = _wait_for_batch(client, auth_headers, batch_id)
    assert batch["status"] == "partial"
    assert batch["failed_videos"] == 1
    assert batch["completed_videos"] == 1

    # the good video still produced clips
    r = client.get(f"/api/clips?batch_id={batch_id}", headers=auth_headers)
    assert len(r.json()) == 1

    # retry the failed job -> still fails (file still missing), retry counted
    r = client.get(f"/api/batches/{batch_id}/jobs", headers=auth_headers)
    failed_job = next(j for j in r.json() if j["status"] == "failed")
    r = client.post(f"/api/jobs/{failed_job['id']}/retry", headers=auth_headers)
    assert r.json()["ok"]
    _wait_for_batch(client, auth_headers, batch_id, timeout=60)


def test_job_state_persistence(client, auth_headers, uploaded_video, stub_transcription):
    """Job records persist with progress and stages across the run."""
    r = client.post("/api/batches", headers=auth_headers, json={
        "name": "Persist Batch", "video_ids": [uploaded_video["id"]],
        "clips_per_video": 1, "duration": {"min_seconds": 8, "max_seconds": 12},
    })
    batch_id = r.json()["id"]
    _wait_for_batch(client, auth_headers, batch_id)
    r = client.get(f"/api/batches/{batch_id}/jobs", headers=auth_headers)
    jobs = r.json()
    assert len(jobs) == 1
    job = jobs[0]
    assert job["status"] == "done"
    assert job["progress"] == 1.0
    assert job["started_at"] and job["finished_at"]


def test_transcribe_endpoint_with_stub(client, auth_headers, uploaded_video, stub_transcription):
    r = client.post(f"/api/videos/{uploaded_video['id']}/transcribe", headers=auth_headers)
    assert r.status_code == 200
    r = client.get(f"/api/videos/{uploaded_video['id']}/transcript", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["language"] == "en"
    assert len(body["segments"]) == len(FAKE_SEGMENTS)
    # transcript persisted: second call still works (and would be reused by batches)
    r = client.get(f"/api/videos/{uploaded_video['id']}/transcript", headers=auth_headers)
    assert r.status_code == 200
