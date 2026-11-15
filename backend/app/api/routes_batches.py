"""Batch, job, clip and export routes."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..config import settings
from ..db import get_db
from ..models import Batch, Clip, ClipVersion, Export, Job, User, Video
from ..schemas import (
    BatchCreateRequest,
    BatchOut,
    ClipCreatedOut,
    ClipOut,
    ClipUpdate,
    ExportOut,
    JobOut,
)
from ..security import ensure_inside, safe_filename
from ..services import export as export_service
from ..services import ffmpeg_engine, jobs, selection

router = APIRouter(prefix="/api", tags=["batches"])


# ---------------------------------------------------------------- batches
@router.post("/batches", response_model=BatchOut)
def api_create_batch(body: BatchCreateRequest, session: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    try:
        batch = jobs.create_batch(session, user.id, body.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    session.flush()
    return batch


@router.get("/batches", response_model=list[BatchOut])
def api_list_batches(session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return session.query(Batch).filter(Batch.user_id == user.id).order_by(Batch.created_at.desc()).all()


@router.get("/batches/{batch_id}", response_model=BatchOut)
def api_get_batch(batch_id: int, session: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    batch = session.get(Batch, batch_id)
    if not batch or batch.user_id != user.id:
        raise HTTPException(404, "Batch not found")
    batch.clip_count = session.query(Clip).filter(Clip.batch_id == batch_id).count()
    return batch


@router.get("/batches/{batch_id}/jobs", response_model=list[JobOut])
def api_batch_jobs(batch_id: int, session: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    batch = session.get(Batch, batch_id)
    if not batch or batch.user_id != user.id:
        raise HTTPException(404, "Batch not found")
    return session.query(Job).filter(Job.batch_id == batch_id).order_by(Job.id).all()


@router.post("/batches/{batch_id}/pause")
def api_pause_batch(batch_id: int, session: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    if not jobs.pause_batch(session, user.id, batch_id):
        raise HTTPException(404, "Batch not found")
    return {"ok": True}


@router.post("/batches/{batch_id}/resume")
def api_resume_batch(batch_id: int, session: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    if not jobs.resume_batch(session, user.id, batch_id):
        raise HTTPException(404, "Batch not found")
    return {"ok": True}


@router.post("/batches/{batch_id}/cancel")
def api_cancel_batch(batch_id: int, session: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    if not jobs.cancel_batch(session, user.id, batch_id):
        raise HTTPException(404, "Batch not found")
    return {"ok": True}


@router.post("/jobs/{job_id}/retry")
def api_retry_job(job_id: int, session: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    if not jobs.retry_job(session, user.id, job_id):
        raise HTTPException(404, "Job not found")
    return {"ok": True}


# ---------------------------------------------------------------- clips
def _clip_out(clip: Clip) -> ClipOut:
    video = clip.video
    return ClipOut(
        id=clip.id, batch_id=clip.batch_id, video_id=clip.video_id,
        video_name=video.name if video else "",
        folder_path=video.folder_path if video else "",
        index=clip.index, start_time=clip.start_time, end_time=clip.end_time,
        duration=round(clip.end_time - clip.start_time, 2),
        hook=clip.hook, title=clip.title, description=clip.description,
        score=clip.score, category=clip.category, reason=clip.reason,
        caption_style=clip.caption_style, layout=clip.layout, handle=clip.handle,
        status=clip.status, is_heuristic=clip.is_heuristic,
        current_version=clip.current_version,
        versions=[_version_out(v) for v in clip.versions],
        created_at=clip.created_at,
    )


def _version_out(v: ClipVersion):
    from ..schemas import ClipVersionOut

    return ClipVersionOut(
        id=v.id, version=v.version, render_path=v.render_path, thumbnail_path=v.thumbnail_path,
        duration=v.duration, size=v.size, status=v.status, created_at=v.created_at,
    )


@router.get("/clips", response_model=list[ClipOut])
def api_list_clips(batch_id: int | None = None, video_id: int | None = None,
                   status: str | None = None, session: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    q = session.query(Clip).filter(Clip.user_id == user.id)
    if batch_id:
        q = q.filter(Clip.batch_id == batch_id)
    if video_id:
        q = q.filter(Clip.video_id == video_id)
    if status:
        q = q.filter(Clip.status == status)
    return [_clip_out(c) for c in q.order_by(Clip.created_at.desc()).all()]


@router.get("/clips/{clip_id}", response_model=ClipOut)
def api_get_clip(clip_id: int, session: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    clip = session.get(Clip, clip_id)
    if not clip or clip.user_id != user.id:
        raise HTTPException(404, "Clip not found")
    return _clip_out(clip)


@router.post("/clips/{clip_id}/render", response_model=ClipCreatedOut)
def api_render_clip(clip_id: int, session: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """Render (or re-render) a clip as a new version."""
    clip = session.get(Clip, clip_id)
    if not clip or clip.user_id != user.id:
        raise HTTPException(404, "Clip not found")
    video = session.get(Video, clip.video_id)
    if not video or not video.local_path or not Path(video.local_path).exists():
        raise HTTPException(400, "Source video is not available locally")
    from ..services.jobs import _render_clip_record

    job = session.get(Job, clip.batch_id) if clip.batch_id else None
    try:
        _render_clip_record(session, clip, video, {"start": clip.start_time, "end": clip.end_time},
                            clip.handle, job or Job(id=0), clip.index, 1)
    except Exception as exc:
        session.flush()
        raise HTTPException(500, f"Render failed: {exc}")
    session.flush()
    return ClipCreatedOut(id=clip.id, status=clip.status, message="Render completed")


@router.put("/clips/{clip_id}", response_model=ClipOut)
def api_update_clip(clip_id: int, body: ClipUpdate, session: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    clip = session.get(Clip, clip_id)
    if not clip or clip.user_id != user.id:
        raise HTTPException(404, "Clip not found")
    video = session.get(Video, clip.video_id)
    dur = video.duration if video else 0
    data = body.model_dump(exclude_none=True)
    for key, val in data.items():
        setattr(clip, key, val)
    # validate timestamps
    if clip.end_time <= clip.start_time:
        raise HTTPException(400, "end_time must be greater than start_time")
    if clip.end_time > dur + 1:
        raise HTTPException(400, f"end_time exceeds source duration ({dur:.1f}s)")
    clip.status = "selected"  # requires re-render
    session.flush()
    return _clip_out(clip)


@router.delete("/clips/{clip_id}")
def api_delete_clip(clip_id: int, session: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    clip = session.get(Clip, clip_id)
    if not clip or clip.user_id != user.id:
        raise HTTPException(404, "Clip not found")
    for v in clip.versions:
        if v.render_path and Path(v.render_path).exists():
            try:
                Path(v.render_path).unlink()
            except OSError:
                pass
        if v.thumbnail_path and Path(v.thumbnail_path).exists():
            try:
                Path(v.thumbnail_path).unlink()
            except OSError:
                pass
    session.delete(clip)
    session.flush()
    return {"ok": True}


@router.get("/clips/{clip_id}/download")
def api_download_clip(clip_id: int, session: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    clip = session.get(Clip, clip_id)
    if not clip or clip.user_id != user.id:
        raise HTTPException(404, "Clip not found")
    ver = clip.versions[-1] if clip.versions else None
    if not ver or ver.status != "rendered" or not ver.render_path:
        raise HTTPException(404, "Clip has no rendered output")
    path = ensure_inside(settings.data_path, Path(ver.render_path))
    name = f"clip_{clip.id}_{safe_filename(clip.title or 'clip')}.mp4"
    return FileResponse(path, filename=name, media_type="video/mp4")


@router.get("/clips/{clip_id}/thumbnail")
def api_clip_thumbnail(clip_id: int, session: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    clip = session.get(Clip, clip_id)
    if not clip or clip.user_id != user.id:
        raise HTTPException(404, "Clip not found")
    ver = clip.versions[-1] if clip.versions else None
    if not ver or not ver.thumbnail_path or not Path(ver.thumbnail_path).exists():
        raise HTTPException(404, "No thumbnail available")
    path = ensure_inside(settings.data_path, Path(ver.thumbnail_path))
    return FileResponse(path, media_type="image/jpeg")


# ---------------------------------------------------------------- exports
@router.post("/batches/{batch_id}/export", response_model=ExportOut)
def api_create_export(batch_id: int, session: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    batch = session.get(Batch, batch_id)
    if not batch or batch.user_id != user.id:
        raise HTTPException(404, "Batch not found")
    try:
        export = export_service.build_batch_zip(session, user.id, batch_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    session.flush()
    return export


@router.get("/exports", response_model=list[ExportOut])
def api_list_exports(session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return session.query(Export).filter(Export.user_id == user.id).order_by(Export.created_at.desc()).all()


@router.get("/exports/{export_id}/download")
def api_download_export(export_id: int, session: Session = Depends(get_db),
                        user: User = Depends(get_current_user)):
    export = session.get(Export, export_id)
    if not export or export.user_id != user.id:
        raise HTTPException(404, "Export not found")
    if export.status != "done" or not export.zip_path:
        raise HTTPException(409, "Export is not ready")
    path = ensure_inside(settings.data_path, Path(export.zip_path))
    batch = session.get(Batch, export.batch_id)
    name = f"ArcusClips_Batch_{export.batch_id:03d}.zip"
    if batch and batch.name:
        name = f"ArcusClips_{safe_filename(batch.name)}.zip"
    return FileResponse(path, filename=name, media_type="application/zip")
