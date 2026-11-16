"""Persistent job queue and batch orchestration.

Jobs are DB-backed and recoverable. One job per source video inside a batch
plus scan/import jobs. Stages: pending → importing → ffprobe → transcription
→ analysis → selection → rendering → done/failed/cancelled. A background
worker (threads, bounded concurrency) executes jobs; heartbeats allow stale
job recovery after restarts.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.orm import Session

from ..config import settings
from ..db import utcnow
from ..models import Batch, Clip, ClipVersion, Event, Job, Source, Transcript, Video
from ..security import safe_filename
from . import ffmpeg_engine, lmstudio, selection, whisper_service

logger = logging.getLogger("arcusclips.jobs")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def log_event(session: Session, user_id: int | None, level: str, message: str,
              source: str = "jobs") -> None:
    session.add(Event(user_id=user_id, level=level, source=source, message=message[:1000]))


def _checkpoint(session: Session) -> None:
    """Commit the current transaction and release the SQLite write lock.

    The worker holds one session for the whole lifetime of a job, and SQLite
    allows only one writer at a time. Committing at phase boundaries keeps the
    write lock held for milliseconds instead of the minutes a download or
    render can take — otherwise API writes (pause/cancel/create batch) block
    on the busy timeout and fail with 'database is locked'. get_session_factory
    uses expire_on_commit=False, so callers keep using their instances.
    """
    session.commit()


def _update_job(session: Session, job: Job, status: str | None = None,
                stage: str | None = None, progress: float | None = None,
                error: str | None = None) -> None:
    if status:
        job.status = status
        if status in ("done", "failed", "cancelled"):
            job.finished_at = utcnow()
    if stage:
        job.stage = stage
    if progress is not None:
        job.progress = max(0.0, min(1.0, progress))
    if error is not None:
        job.error = error[:4000]
    job.heartbeat_at = utcnow()
    _checkpoint(session)


def compute_batch_status(session: Session, batch: Batch) -> None:
    """Recompute batch aggregate state from its jobs."""
    jobs = session.query(Job).filter(Job.batch_id == batch.id).all()
    total = len(jobs)
    done = sum(1 for j in jobs if j.status == "done")
    failed = sum(1 for j in jobs if j.status == "failed")
    cancelled = sum(1 for j in jobs if j.status == "cancelled")
    active = sum(1 for j in jobs if j.status in ("running", "pending", "paused"))
    batch.total_videos = total
    batch.completed_videos = done
    batch.failed_videos = failed
    if batch.status in ("cancelled",):
        return
    if active == 0:
        if failed and done:
            batch.status = "partial"
        elif failed and not done:
            batch.status = "failed"
        else:
            batch.status = "completed"
        batch.finished_at = utcnow()
    elif any(j.status == "running" for j in jobs):
        batch.status = {
            "importing": "downloading",
            "ffprobe": "downloading",
            "transcription": "transcribing",
            "analysis": "analyzing",
            "selection": "selecting",
            "rendering": "rendering",
        }.get(next((j.stage for j in jobs if j.status == "running"), ""), "processing")
    else:
        batch.status = "queued"


# ---------------------------------------------------------------------------
# Batch creation
# ---------------------------------------------------------------------------
def create_batch(session: Session, user_id: int, options: dict[str, Any]) -> Batch:
    duration_opts = options.get("duration") or {}
    min_sec = float(duration_opts.get("min_seconds", 15))
    max_sec = float(duration_opts.get("max_seconds", 30))
    if max_sec <= min_sec:
        max_sec = min_sec + 15

    # Server-side validation of batch options (not only the frontend).
    clips_per_video = int(options.get("clips_per_video", 3))
    if clips_per_video < 1:
        raise ValueError("clips_per_video must be at least 1")
    max_total = int(options.get("max_total_clips", 0) or 0)
    if max_total < 0:
        raise ValueError("max_total_clips must be 0 (unlimited) or a positive integer")
    if min_sec < 1:
        raise ValueError("duration.min_seconds must be at least 1")

    # resolve the video set
    video_ids = [int(v) for v in options.get("video_ids", []) if v]
    source_ids = [int(s) for s in options.get("source_ids", []) if s]
    query = session.query(Video).filter(Video.user_id == user_id)
    if video_ids:
        query = query.filter(Video.id.in_(video_ids))
    elif source_ids:
        query = query.filter(Video.source_id.in_(source_ids))
    else:
        query = query.filter(Video.status.in_(["imported", "discovered"]))
    videos = query.all()

    skip_processed = bool(options.get("skip_processed", True))
    if skip_processed:
        videos = [v for v in videos if v.status != "processed"]

    if not videos:
        raise ValueError("No eligible videos selected for this batch")

    name = options.get("name") or f"Batch {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}"
    batch = Batch(
        user_id=user_id,
        name=name,
        status="queued",
        options={
            **options,
            "clips_per_video": clips_per_video,
            "duration": {"min_seconds": min_sec, "max_seconds": max_sec},
        },
    )
    session.add(batch)
    session.flush()

    queued_clips = 0
    for v in videos:
        # max_total_clips limits the overall number of clips requested. When it
        # is set, stop queueing once the remaining capacity cannot fit another
        # full clips_per_video batch. A final partial video is included only if
        # it fits within the remaining capacity (handles non-even divisions).
        if max_total > 0:
            remaining = max_total - queued_clips
            if remaining <= 0:
                break
            if remaining < clips_per_video:
                # Only the remaining capacity is requested from this video.
                clips_for_this = remaining
            else:
                clips_for_this = clips_per_video
        else:
            clips_for_this = clips_per_video

        session.add(Job(
            user_id=user_id,
            batch_id=batch.id,
            video_id=v.id,
            source_id=v.source_id,
            kind="render",
            status="pending",
            stage="queued",
            progress=0.0,
            payload={"clips_per_video": clips_for_this},
        ))
        queued_clips += clips_for_this
    session.flush()
    compute_batch_status(session, batch)
    log_event(session, user_id, "info", f"Batch '{name}' created with {batch.total_videos} videos")
    return batch


class _CallableCancelAdapter:
    """Adapts a zero-argument ``cancel_check() -> bool`` callable to the
    ``is_set()`` interface expected by the download paths."""

    def __init__(self, check: Callable[[], bool]):
        self._check = check

    def is_set(self) -> bool:
        try:
            return bool(self._check())
        except Exception:
            # A broken cancel probe must not crash the import; stage-boundary
            # cancel checks and the worker stop event still apply.
            return False


class _ThrottledJobCancelCheck:
    """Cancel probe backed by the worker stop event and the job's row in the DB.

    Fresh DB reads are throttled to at most one per ``interval`` seconds so the
    check stays cheap inside per-chunk download progress hooks, while still
    observing ``cancel_batch()`` writing ``status='cancelled'`` from the API.
    """

    def __init__(self, worker: "Worker | None", job_id: int | None, interval: float = 0.5):
        self._worker = worker
        self._job_id = job_id
        self._interval = interval
        self._last = 0.0
        self._cached = False

    def __call__(self) -> bool:
        worker = self._worker
        if worker is not None and worker._stop.is_set():
            return True
        if self._job_id is None:
            return False
        now = time.monotonic()
        if now - self._last < self._interval:
            return self._cached
        self._last = now
        try:
            from ..db import get_session_factory

            probe = get_session_factory()()
            try:
                status = probe.query(Job.status).filter(Job.id == self._job_id).scalar()
            finally:
                probe.close()
            self._cached = status == "cancelled"
        except Exception:
            # Transient DB errors: keep the last known state.
            pass
        return self._cached


# ---------------------------------------------------------------------------
# Import (Drive download / URL download)
# ---------------------------------------------------------------------------
def ensure_video_imported(session: Session, user_id: int, video: Video, job: Job | None = None,
                          progress_cb: Callable[[float, str], None] | None = None,
                          cancel_event=None) -> Video:
    """Ensure the local file exists for a video, importing/downloading if needed.

    ``cancel_event`` is an object with an ``is_set()`` method (e.g. threading.Event)
    carrying the worker's cancellation signal; it is observed during streaming
    downloads so an in-flight import stops promptly instead of running to
    completion.
    """
    cb = progress_cb or (lambda p, m: None)
    if video.local_path and Path(video.local_path).exists():
        return video

    if video.kind == "drive":
        from . import drive_google

        from ..models import DriveConnection

        conn = session.query(DriveConnection).filter(
            DriveConnection.user_id == user_id,
            DriveConnection.status == "connected",
        ).first()
        if not conn:
            raise RuntimeError("Google Drive is not connected")
        client, _user = drive_google.client_from_connection(conn)
        df = video.drive_file
        if not df:
            raise RuntimeError("Video has no Drive file inventory entry")
        out_dir = settings.video_path / "drive"
        from ..security import safe_relative_path

        safe_rel = safe_relative_path(df.relative_path or df.name)
        out_path = out_dir / safe_rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Use the real target file ID (shortcuts point at a different file);
        # files.get_media() does not accept driveId — the target ID is
        # unambiguous across drives. The stored md5 checksum is passed so the
        # download is validated against the provider's metadata.
        try:
            drive_google.download_file(
                client, df.target_id or df.drive_file_id, out_path, file_size=df.size,
                checksum=df.checksum or "", progress_cb=cb, cancel_event=cancel_event,
            )
        except drive_google.DriveCancelled as exc:
            from . import source_scanner

            raise source_scanner.DownloadCancelled("Download cancelled") from exc
        video.local_path = str(out_path)
        video.status = "imported"
        video.status_detail = "Downloaded from Google Drive"
        video.imported_at = utcnow()
        if df:
            df.import_status = "imported"
        _checkpoint(session)
    elif video.kind in ("url", "page"):
        from . import source_scanner

        out_dir = settings.video_path / "urls"
        out_dir.mkdir(parents=True, exist_ok=True)
        res = source_scanner.import_video_from_url(video.origin_url, str(out_dir), progress_cb=cb,
                                                   cancel_event=cancel_event)
        if not res.get("path") or not Path(res["path"]).exists():
            raise RuntimeError("URL import did not produce a file")
        video.local_path = res["path"]
        video.name = video.name or res.get("title", "")
        video.status = "imported"
        video.status_detail = "Downloaded from URL"
        video.imported_at = utcnow()
        _checkpoint(session)
    else:
        raise RuntimeError(f"Cannot import video of kind '{video.kind}'")
    return video


# ---------------------------------------------------------------------------
# Video job pipeline
# ---------------------------------------------------------------------------
def run_video_job(job: Job, session: Session, render_semaphore: threading.Semaphore,
                  cancel_check: Callable[[], bool] | None = None) -> None:
    """Execute the full clip-generation pipeline for one job."""
    cancel = cancel_check or (lambda: False)
    video = session.get(Video, job.video_id)
    if not video:
        _update_job(session, job, status="failed", error="Video record missing")
        return
    user_id = job.user_id
    options = (job.batch.options if job.batch else {}) or {}
    clips_per_video = int((job.payload or {}).get("clips_per_video",
                       options.get("clips_per_video", 3)))
    duration_opts = options.get("duration") or {}
    min_sec = float(duration_opts.get("min_seconds", 15))
    max_sec = float(duration_opts.get("max_seconds", 30))
    quality = options.get("quality", "balanced")
    style = options.get("caption_style", "bounce")
    layout = options.get("layout", "blur")
    handle = options.get("creator_handle", "")

    try:
        _update_job(session, job, status="running", stage="importing", progress=0.05)
        video = ensure_video_imported(session, user_id, video, job,
                                      cancel_event=_CallableCancelAdapter(cancel))
        if cancel():
            _update_job(session, job, status="cancelled", stage="cancelled")
            return

        # FFprobe validation
        _update_job(session, job, stage="ffprobe", progress=0.15)
        media = ffmpeg_engine.probe(video.local_path)
        if not media.valid:
            raise RuntimeError(f"Invalid media file: {media.detail or 'no video stream'}")
        video.duration = media.duration
        video.width = media.width
        video.height = media.height
        video.has_audio = media.has_audio
        video.size = media.size
        video.media_valid = True
        _checkpoint(session)

        # Transcription
        transcript = session.query(Transcript).filter(Transcript.video_id == video.id).first()
        if transcript is None or not (transcript.segments or []):
            _update_job(session, job, stage="transcription", progress=0.3)
            wav_path = str(settings.transcript_path / f"{video.id}.wav")
            source_hash = whisper_service.file_hash(video.local_path)

            def _tcb(p: float, m: str) -> None:
                job.progress = 0.3 + 0.35 * p
                job.heartbeat_at = utcnow()
                _checkpoint(session)

            if not media.has_audio:
                segments = [{"start": 0.0, "end": media.duration, "text": "", "words": [], "word_count": 0}]
                result = {"language": "unknown", "duration": media.duration, "segments": segments,
                          "model": "silence"}
            else:
                result = whisper_service.transcribe_video_file(
                    video.local_path, wav_path, progress_cb=_tcb
                )
            whisper_service.persist_transcript(video.id, user_id, result, source_hash, session)
            log_event(session, user_id, "info", f"Transcribed video {video.name}")
            transcript = session.query(Transcript).filter(Transcript.video_id == video.id).first()
        _update_job(session, job, progress=0.65)
        if cancel():
            _update_job(session, job, status="cancelled", stage="cancelled")
            return

        # Analysis + selection
        _update_job(session, job, stage="analysis", progress=0.7)
        segments = transcript.segments or []
        selection_result = selection.select_moments(
            video_title=video.name,
            duration=video.duration,
            segments=segments,
            clip_count=clips_per_video,
            min_sec=min_sec,
            max_sec=max_sec,
            quality=quality,
            media_path=video.local_path,
        )
        candidates = selection_result["candidates"]
        if not candidates:
            raise RuntimeError(selection_result.get("error") or "No eligible moments found")
        if selection_result["source"] == "heuristic":
            log_event(session, user_id, "warning",
                      f"Heuristic fallback used for {video.name} (LM Studio unavailable)")
        _update_job(session, job, stage="selection", progress=0.8)

        # Render clips
        is_heuristic = selection_result["source"] == "heuristic"
        _update_job(session, job, stage="rendering", progress=0.8)
        for idx, cand in enumerate(candidates, start=1):
            if cancel():
                _update_job(session, job, status="cancelled", stage="cancelled")
                return
            clip = Clip(
                user_id=user_id,
                batch_id=job.batch_id,
                video_id=video.id,
                index=idx,
                start_time=cand["start"],
                end_time=cand["end"],
                hook=cand.get("hook", ""),
                title=cand.get("title", ""),
                description=cand.get("description", ""),
                score=cand.get("score", 0.0),
                category=cand.get("category", ""),
                reason=cand.get("reason", ""),
                caption_style=style,
                layout=layout,
                handle=handle,
                status="rendering",
                is_heuristic=is_heuristic,
            )
            session.add(clip)
            _checkpoint(session)

            with render_semaphore:
                _render_clip_record(session, clip, video, cand, handle, job, idx, len(candidates))
            clip.status = "rendered" if clip.current_version else "failed"
            _checkpoint(session)

        video.status = "processed"
        video.status_detail = f"{len(candidates)} clips generated"
        _checkpoint(session)
        _update_job(session, job, status="done", stage="done", progress=1.0)
        if job.batch:
            compute_batch_status(session, job.batch)
        log_event(session, user_id, "info", f"Completed video job for {video.name}")

    except Exception as exc:
        from .source_scanner import DownloadCancelled

        if isinstance(exc, DownloadCancelled):
            # Cancellation requested while the import was in flight — keep the
            # existing cancellation semantics (status cancelled, batch recomputed).
            logger.info("Job %s cancelled during import", job.id)
            _update_job(session, job, status="cancelled", stage="cancelled", error="Cancelled")
            log_event(session, user_id, "info", f"Job cancelled for {video.name if video else job.id}")
            if job.batch:
                compute_batch_status(session, job.batch)
            return
        logger.error("Job %s failed: %s\n%s", job.id, exc, traceback.format_exc())
        _update_job(session, job, status="failed", stage="failed", error=str(exc))
        log_event(session, user_id, "error", f"Job failed for {video.name if video else job.id}: {exc}")
        if job.batch:
            compute_batch_status(session, job.batch)


def _render_clip_record(session: Session, clip: Clip, video: Video, cand: dict[str, Any],
                        handle: str, job: Job, idx: int, total: int) -> None:
    """Render one clip to a new version. Preserves previous versions."""
    version = clip.current_version + 1
    clip.current_version = version
    safe = safe_filename(video.name, fallback="video")
    out_path = settings.render_path / f"batch{job.batch_id or 0}" / f"clip{clip.id}_v{version}_{safe}.mp4"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    transcript = session.query(Transcript).filter(Transcript.video_id == video.id).first()
    segments = transcript.segments if transcript else []
    words = selection_words(segments, clip.start_time, clip.end_time)

    job.progress = 0.8 + 0.2 * (idx / max(total, 1))
    job.heartbeat_at = utcnow()
    _checkpoint(session)

    ver = ClipVersion(
        clip_id=clip.id, user_id=clip.user_id, version=version,
        status="rendering",
    )
    session.add(ver)
    _checkpoint(session)
    try:
        result = ffmpeg_engine.render_clip(
            video.local_path, clip.start_time, clip.end_time, str(out_path),
            style=clip.caption_style, layout=clip.layout, handle=clip.handle,
            hook=clip.hook, words=words,
        )
        if not result["ok"]:
            ver.status = "failed"
            ver.error = result.get("error", "render failed")
            raise RuntimeError(result.get("error", "render failed"))
        ver.render_path = result["render_path"]
        ver.duration = result["duration"]
        ver.size = result["size"]
        ver.status = "rendered"
        try:
            thumb = settings.render_path / f"batch{job.batch_id or 0}" / f"clip{clip.id}_v{version}.jpg"
            ffmpeg_engine.make_thumbnail(video.local_path, (clip.start_time + clip.end_time) / 2, str(thumb))
            ver.thumbnail_path = str(thumb)
        except Exception:
            pass  # thumbnail is best-effort
        _checkpoint(session)
    except Exception as exc:
        ver.status = "failed"
        ver.error = str(exc)[:2000]
        _checkpoint(session)
        raise


def selection_words(segments: list[dict[str, Any]], start: float, end: float) -> list[tuple[str, float, float]]:
    return ffmpeg_engine._words_from_segments(segments, start, end)


# ---------------------------------------------------------------------------
# Job queue operations
# ---------------------------------------------------------------------------
def requeue_stale_jobs(session: Session) -> int:
    """Recover jobs stuck in 'running' after a restart/crash."""
    stale_before = utcnow() - timedelta(seconds=settings.job_stale_timeout)
    stale = session.query(Job).filter(
        Job.status == "running",
        Job.heartbeat_at < stale_before,
    ).all()
    for job in stale:
        job.status = "pending"
        job.stage = "queued"
        job.progress = 0.0
        job.error = "Recovered after worker restart"
        logger.info("Recovered stale job %s", job.id)
    if stale:
        session.flush()
    return len(stale)


def cancel_batch(session: Session, user_id: int, batch_id: int) -> bool:
    batch = session.get(Batch, batch_id)
    if not batch or batch.user_id != user_id:
        return False
    for job in session.query(Job).filter(Job.batch_id == batch.id).all():
        if job.status in ("pending", "running", "paused"):
            job.status = "cancelled"
            job.stage = "cancelled"
    batch.status = "cancelled"
    batch.finished_at = utcnow()
    session.flush()
    return True


def pause_batch(session: Session, user_id: int, batch_id: int) -> bool:
    batch = session.get(Batch, batch_id)
    if not batch or batch.user_id != user_id:
        return False
    for job in session.query(Job).filter(Job.batch_id == batch.id).all():
        if job.status in ("pending", "running"):
            job.status = "paused"
    batch.status = "paused"
    session.flush()
    return True


def resume_batch(session: Session, user_id: int, batch_id: int) -> bool:
    batch = session.get(Batch, batch_id)
    if not batch or batch.user_id != user_id:
        return False
    for job in session.query(Job).filter(Job.batch_id == batch.id).all():
        if job.status == "paused":
            job.status = "pending"
    compute_batch_status(session, batch)
    session.flush()
    return True


def retry_job(session: Session, user_id: int, job_id: int) -> bool:
    job = session.get(Job, job_id)
    if not job or job.user_id != user_id:
        return False
    job.status = "pending"
    job.stage = "queued"
    job.progress = 0.0
    job.error = ""
    job.retry_count += 1
    if job.batch:
        compute_batch_status(session, job.batch)
    session.flush()
    return True


def next_pending_job(session: Session) -> Job | None:
    return (
        session.query(Job)
        .filter(Job.status == "pending")
        .order_by(Job.created_at)
        .first()
    )


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------
class Worker:
    """Background worker: polls the DB queue and runs jobs with bounded concurrency."""

    def __init__(self, worker_id: str = ""):
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self._stop = threading.Event()
        self._loop_thread: threading.Thread | None = None
        self._job_threads: list[threading.Thread] = []
        self._active = 0
        self._lock = threading.Lock()
        self._render_sem = threading.Semaphore(max(1, settings.max_render_concurrency))

    def start(self) -> None:
        self._stop.clear()
        self._loop_thread = threading.Thread(
            target=self._loop, name=f"arcus-worker-{self.worker_id}", daemon=True
        )
        self._loop_thread.start()
        logger.info("Worker %s started", self.worker_id)
        set_current_worker(self)

    def stop(self, timeout: float = 60.0) -> None:
        self._stop.set()
        if self._loop_thread:
            self._loop_thread.join(timeout=timeout)
        with self._lock:
            jobs = list(self._job_threads)
        for t in jobs:
            t.join(timeout=timeout)

    @property
    def active_jobs(self) -> int:
        with self._lock:
            return self._active

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                from ..db import get_session_factory

                session = get_session_factory()()
                try:
                    requeue_stale_jobs(session)
                    session.commit()
                    # how many slots remain
                    with self._lock:
                        slots = max(0, settings.max_processing_concurrency - self._active)
                    for _ in range(slots):
                        job = next_pending_job(session)
                        if not job:
                            break
                        job.worker = self.worker_id
                        job.status = "running"
                        job.stage = "importing"
                        job.started_at = utcnow()
                        job.heartbeat_at = utcnow()
                        session.flush()
                        if job.batch_id:
                            compute_batch_status(session, job.batch)
                        session.commit()
                        with self._lock:
                            self._active += 1
                            self._job_threads.append(threading.Thread(
                                target=self._run_one, args=(job.id,), daemon=True
                            ))
                            t = self._job_threads[-1]
                        t.start()
                finally:
                    session.close()
            except Exception:
                logger.exception("Worker loop error")
            self._stop.wait(settings.job_poll_interval)

    def _run_one(self, job_id: int) -> None:
        if self._stop.is_set():
            return
        try:
            from ..db import get_session_factory

            session = get_session_factory()()
            try:
                job = session.get(Job, job_id)
                if not job or job.status != "running":
                    return
                cancel_probe = _ThrottledJobCancelCheck(self, job.id)
                run_video_job(job, session, self._render_sem,
                              cancel_check=cancel_probe)
                session.commit()
            finally:
                session.close()
        finally:
            with self._lock:
                self._active -= 1


# ---------------------------------------------------------------------------
# Module-level handle to the active worker, so the API can report its state
# without importing app.main (and without fragile function attributes).
# ---------------------------------------------------------------------------
_current_worker: "Worker | None" = None
_current_worker_lock = threading.Lock()


def set_current_worker(worker: "Worker | None") -> None:
    global _current_worker
    with _current_worker_lock:
        _current_worker = worker


def get_current_worker() -> "Worker | None":
    with _current_worker_lock:
        return _current_worker
