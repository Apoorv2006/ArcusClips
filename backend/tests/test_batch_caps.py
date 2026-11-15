"""Regression tests for the bulk batch `max_total_clips` cap.

Verifies the cap counts clips (clips_per_video per video) rather than videos,
respects the cap in the queued job count, handles a partial final video, leaves
no jobs on an empty selection, and skips already-processed videos.
"""
import shutil
from pathlib import Path

import pytest

from app.db import get_session_factory
from app.models import Job


def _upload_videos(client, headers, sample_video, count):
    ids = []
    for i in range(count):
        src = Path(sample_video)
        dest = src.with_name(f"cap_source_{i}.mp4")
        shutil.copy(src, dest)
        with open(dest, "rb") as fh:
            r = client.post(
                "/api/videos/upload",
                files={"file": (dest.name, fh, "video/mp4")},
                headers=headers,
            )
        assert r.status_code == 200, r.text
        ids.append(r.json()["id"])
    return ids


def _batch_jobs(batch_id):
    """Return (batch, jobs) with each job's payload, read from the DB."""
    session = get_session_factory()()
    try:
        from app.models import Batch

        batch = session.get(Batch, batch_id)
        jobs = session.query(Job).filter(Job.batch_id == batch_id).order_by(Job.id).all()
        return batch, [{"payload": j.payload or {}} for j in jobs]
    finally:
        session.close()


def test_batch_unlimited_cap_queues_all(client, auth_headers, sample_video):
    ids = _upload_videos(client, auth_headers, sample_video, 2)
    r = client.post("/api/batches", headers=auth_headers, json={
        "name": "unlimited", "video_ids": ids,
        "clips_per_video": 3, "max_total_clips": 0,
        "duration": {"min_seconds": 8, "max_seconds": 12},
    })
    assert r.status_code == 200, r.text
    batch, jobs = _batch_jobs(r.json()["id"])
    # one render job per video; each renders clips_per_video clips
    assert batch.total_videos == 2
    assert len(jobs) == 2
    total_clips = sum(j["payload"]["clips_per_video"] for j in jobs)
    assert total_clips == 6


def test_batch_cap_counts_clips_not_videos(client, auth_headers, sample_video):
    """A cap that fits whole videos but not all clips is counted in clips."""
    ids = _upload_videos(client, auth_headers, sample_video, 3)
    # 3 videos * 3 clips = 9 clips requested, cap at 5 clips -> 2 videos fully
    # (6 clips) don't fit, so 2 videos * 3 = 6 also exceeds 5... cap fits 1
    # full video (3 clips) + remaining 2 clips from the next video.
    r = client.post("/api/batches", headers=auth_headers, json={
        "name": "capped", "video_ids": ids,
        "clips_per_video": 3, "max_total_clips": 5,
        "duration": {"min_seconds": 8, "max_seconds": 12},
    })
    assert r.status_code == 200, r.text
    batch, jobs = _batch_jobs(r.json()["id"])
    queued_clips = sum(j["payload"]["clips_per_video"] for j in jobs)
    assert queued_clips == 5
    assert len(jobs) == 2  # one full video + one partial
    # payload records the (possibly reduced) clips requested for each job
    assert sorted(j["payload"]["clips_per_video"] for j in jobs) == [2, 3]
    assert batch.total_videos == 2


def test_batch_cap_below_clips_per_video(client, auth_headers, sample_video):
    ids = _upload_videos(client, auth_headers, sample_video, 2)
    # cap smaller than clips_per_video: only the remainder is requested
    r = client.post("/api/batches", headers=auth_headers, json={
        "name": "cap-below", "video_ids": ids,
        "clips_per_video": 5, "max_total_clips": 2,
        "duration": {"min_seconds": 8, "max_seconds": 12},
    })
    assert r.status_code == 200, r.text
    batch, jobs = _batch_jobs(r.json()["id"])
    queued_clips = sum(j["payload"]["clips_per_video"] for j in jobs)
    assert queued_clips == 2
    assert len(jobs) == 1


def test_batch_cap_fewer_videos_than_cap(client, auth_headers, sample_video):
    ids = _upload_videos(client, auth_headers, sample_video, 1)
    r = client.post("/api/batches", headers=auth_headers, json={
        "name": "fewer", "video_ids": ids,
        "clips_per_video": 2, "max_total_clips": 10,
        "duration": {"min_seconds": 8, "max_seconds": 12},
    })
    assert r.status_code == 200, r.text
    batch, jobs = _batch_jobs(r.json()["id"])
    # cap (10) exceeds available clips (2) -> the single video is queued
    assert len(jobs) == 1
    assert sum(j["payload"]["clips_per_video"] for j in jobs) == 2


def test_batch_cap_exactly_matches(client, auth_headers, sample_video):
    ids = _upload_videos(client, auth_headers, sample_video, 2)
    # 2 videos * 3 clips = 6, cap exactly 6 -> all queued
    r = client.post("/api/batches", headers=auth_headers, json={
        "name": "exact", "video_ids": ids,
        "clips_per_video": 3, "max_total_clips": 6,
        "duration": {"min_seconds": 8, "max_seconds": 12},
    })
    assert r.status_code == 200, r.text
    batch, jobs = _batch_jobs(r.json()["id"])
    assert len(jobs) == 2
    assert sum(j["payload"]["clips_per_video"] for j in jobs) == 6


def test_batch_empty_selection_creates_no_jobs(client, auth_headers):
    r = client.post("/api/batches", headers=auth_headers, json={
        "name": "empty", "video_ids": [],
        "clips_per_video": 3, "duration": {"min_seconds": 8, "max_seconds": 12},
    })
    # empty selection is rejected before any job is created
    assert r.status_code in (400, 422)


def test_batch_skip_processed(client, auth_headers, sample_video):
    ids = _upload_videos(client, auth_headers, sample_video, 2)
    # mark first video as processed directly in the DB
    session = get_session_factory()()
    try:
        from app.models import Video

        v = session.get(Video, ids[0])
        v.status = "processed"
        session.commit()
    finally:
        session.close()
    r = client.post("/api/batches", headers=auth_headers, json={
        "name": "skip", "video_ids": ids,
        "clips_per_video": 3, "skip_processed": True,
        "duration": {"min_seconds": 8, "max_seconds": 12},
    })
    assert r.status_code == 200, r.text
    batch, jobs = _batch_jobs(r.json()["id"])
    # only the unprocessed video is queued (one render job per video)
    assert len(jobs) == 1
    assert jobs[0]["payload"]["clips_per_video"] == 3


def test_batch_validation_rejects_invalid_clips_per_video(client, auth_headers, sample_video):
    ids = _upload_videos(client, auth_headers, sample_video, 1)
    r = client.post("/api/batches", headers=auth_headers, json={
        "name": "bad", "video_ids": ids,
        "clips_per_video": 0,
        "duration": {"min_seconds": 8, "max_seconds": 12},
    })
    assert r.status_code in (400, 422)


def test_batch_validation_rejects_negative_cap(client, auth_headers, sample_video):
    ids = _upload_videos(client, auth_headers, sample_video, 1)
    r = client.post("/api/batches", headers=auth_headers, json={
        "name": "badcap", "video_ids": ids,
        "clips_per_video": 2, "max_total_clips": -1,
        "duration": {"min_seconds": 8, "max_seconds": 12},
    })
    assert r.status_code in (400, 422)
