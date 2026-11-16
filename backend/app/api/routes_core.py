"""Core API routes: auth, settings, dashboard."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import get_current_user, login
from ..config import settings
from ..db import get_db
from ..models import AppSetting, Batch, Clip, Event, Job, Source, User, Video
from ..schemas import (
    DashboardStats,
    LMStudioModelsResponse,
    LoginRequest,
    TokenResponse,
    UserOut,
    SettingsUpdate,
    WorkerStatus,
)
from ..services import ffmpeg_engine, lmstudio, whisper_service

router = APIRouter(prefix="/api", tags=["core"])

_SETTING_KEYS = {
    "lmstudio": ("LM Studio",),
    "whisper": ("Whisper",),
    "render": ("Render",),
    "processing": ("Processing",),
    "branding": ("Branding",),
}


# ---------------------------------------------------------------- auth
@router.post("/auth/login", response_model=TokenResponse)
def api_login(body: LoginRequest, session: Session = Depends(get_db)):
    token, user = login(session, body.username, body.password)
    return TokenResponse(token=token, user_id=user.id, username=user.username, is_admin=user.is_admin)


@router.get("/auth/me", response_model=UserOut)
def api_me(user: User = Depends(get_current_user)):
    return user


# ---------------------------------------------------------------- settings
def _get_setting(session: Session, key: str) -> dict[str, Any]:
    row = session.get(AppSetting, key)
    if not row or not row.value:
        return {}
    return dict(row.value)


def _save_setting(session: Session, key: str, value: dict[str, Any], user_id: int | None):
    row = session.get(AppSetting, key)
    if not row:
        row = AppSetting(key=key, value=value, user_id=user_id)
        session.add(row)
    else:
        row.value = value
    session.flush()


@router.get("/settings")
def api_get_settings(session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    current = {
        "lmstudio": {
            "base_url": settings.lmstudio_base_url,
            "model": _get_setting(session, "lmstudio").get("model", settings.lmstudio_model),
            "api_key": "",  # never expose stored API key
            "request_timeout": settings.lmstudio_timeout,
            "max_tokens": settings.lmstudio_max_tokens,
            "temperature": settings.lmstudio_temperature,
            "max_context_tokens": settings.lmstudio_max_context_tokens,
        },
        "whisper": {
            "model": _get_setting(session, "whisper").get("model", settings.whisper_model),
            "model_dir": _get_setting(session, "whisper").get("model_dir", ""),
            "device": settings.whisper_device,
            "compute_type": settings.whisper_compute_type,
            "language": settings.whisper_language,
        },
        "render": {
            "width": settings.render_width,
            "height": settings.render_height,
            "fps": settings.render_fps,
            "crf": settings.render_crf,
            "preset": settings.render_preset,
        },
        "processing": {
            "max_processing_concurrency": settings.max_processing_concurrency,
            "max_render_concurrency": settings.max_render_concurrency,
            "max_download_concurrency": settings.max_download_concurrency,
            "allow_heuristic_fallback": settings.allow_heuristic_fallback,
        },
        "branding": {
            "creator_handle": _get_setting(session, "branding").get("creator_handle", ""),
            "default_caption_style": _get_setting(session, "branding").get("default_caption_style", "bounce"),
            "default_layout": _get_setting(session, "branding").get("default_layout", "blur"),
            "font_name": _get_setting(session, "branding").get("font_name", ""),
        },
    }
    return current


@router.put("/settings")
def api_update_settings(body: SettingsUpdate, session: Session = Depends(get_db),
                        user: User = Depends(get_current_user)):
    if body.lmstudio:
        val = body.lmstudio.model_dump()
        _save_setting(session, "lmstudio", val, user.id)
    if body.whisper:
        _save_setting(session, "whisper", body.whisper.model_dump(), user.id)
    if body.render:
        _save_setting(session, "render", body.render.model_dump(), user.id)
    if body.processing:
        _save_setting(session, "processing", body.processing.model_dump(), user.id)
    if body.branding:
        _save_setting(session, "branding", body.branding.model_dump(), user.id)
    return {"ok": True}


@router.get("/settings/lmstudio/models", response_model=LMStudioModelsResponse)
def api_lmstudio_models(session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    stored = _get_setting(session, "lmstudio")
    # apply stored settings into runtime config for this call
    if stored:
        for k, v in stored.items():
            if hasattr(settings, k) and v:
                setattr(settings, k, v)
    result = lmstudio.health()
    return LMStudioModelsResponse(connected=result["connected"], models=result["models"],
                                  error=result["error"])


@router.post("/settings/lmstudio/test", response_model=LMStudioModelsResponse)
def api_lmstudio_test(session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    stored = _get_setting(session, "lmstudio")
    if stored:
        for k, v in stored.items():
            if hasattr(settings, k) and v:
                setattr(settings, k, v)
    result = lmstudio.health()
    return LMStudioModelsResponse(connected=result["connected"], models=result["models"],
                                  error=result["error"])


@router.get("/settings/ffmpeg")
def api_ffmpeg_status(user: User = Depends(get_current_user)):
    return {"ffmpeg_version": ffmpeg_engine.ffmpeg_version(),
            "ffmpeg_path": ffmpeg_engine.get_ffmpeg_path(),
            "ffprobe_path": ffmpeg_engine.get_ffprobe_path() or "",
            "ffprobe": ffmpeg_engine.ffprobe_status(),
            "caption_styles": list(ffmpeg_engine.CAPTION_STYLES),
            "layouts": list(ffmpeg_engine.LAYOUTS)}


# ---------------------------------------------------------------- dashboard
@router.get("/dashboard", response_model=DashboardStats)
def api_dashboard(session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    def _count(model, **filters):
        q = session.query(model).filter(model.user_id == user.id)
        for k, v in filters.items():
            q = q.filter(getattr(model, k) == v)
        return q.count()

    total_videos = session.query(Video).filter(Video.user_id == user.id).count()
    imported = _count(Video, status="imported") + _count(Video, status="processed")
    processed = _count(Video, status="processed")
    storage_bytes = int(
        session.query(func.coalesce(func.sum(Video.size), 0))
        .filter(Video.user_id == user.id)
        .scalar()
        or 0
    )
    active_batches = _count(Batch, status="queued") + _count(Batch, status="processing")
    if active_batches == 0:
        active_batches = sum(
            1 for b in session.query(Batch).filter(Batch.user_id == user.id).all()
            if b.status not in ("completed", "failed", "cancelled")
        )
    failed_jobs = _count(Job, status="failed")
    return DashboardStats(
        sources_connected=_count(Source, monitoring=True),
        discovered_videos=_count(Video, status="discovered"),
        imported_videos=imported,
        processed_videos=processed,
        clips_generated=session.query(Clip).filter(Clip.user_id == user.id).count(),
        active_batches=active_batches,
        failed_jobs=failed_jobs,
        storage_bytes=storage_bytes,
        storage_limit=settings.max_storage_bytes,
        workers_available=lmstudio.is_available() is not None,
        lmstudio_connected=lmstudio.is_available(),
    )


@router.get("/worker/status", response_model=WorkerStatus)
def api_worker_status(user: User = Depends(get_current_user)):
    from ..services.jobs import get_current_worker

    w = get_current_worker()
    return WorkerStatus(
        running=w is not None,
        active_jobs=w.active_jobs if w else 0,
        capacity=settings.max_processing_concurrency,
        poll_interval=settings.job_poll_interval,
    )


@router.get("/logs")
def api_logs(limit: int = 200, session: Session = Depends(get_db),
             user: User = Depends(get_current_user)):
    events = (
        session.query(Event)
        .filter(Event.user_id == user.id)
        .order_by(Event.created_at.desc())
        .limit(min(limit, 1000))
        .all()
    )
    return [
        {"id": e.id, "level": e.level, "source": e.source, "message": e.message,
         "created_at": e.created_at}
        for e in events
    ]
