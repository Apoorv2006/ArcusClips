"""Source + Drive + video routes."""
from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..config import settings
from ..db import get_db, utcnow
from ..models import DriveConnection, DriveFile, Job, Source, Transcript, User, Video
from ..schemas import (
    DriveConnectStatus,
    DriveFolderNode,
    DriveScanRequest,
    SourceCreate,
    SourceOut,
    TranscriptOut,
    VideoOut,
)
from ..security import safe_filename, validate_public_url
from ..services import drive_google, ffmpeg_engine, jobs, source_scanner, whisper_service

router = APIRouter(prefix="/api", tags=["sources"])


# ---------------------------------------------------------------- sources
@router.get("/sources", response_model=list[SourceOut])
def api_list_sources(session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = session.query(Source).filter(Source.user_id == user.id).order_by(Source.created_at.desc()).all()
    for s in rows:
        s.discovered_count = session.query(Video).filter(
            Video.user_id == user.id, Video.source_id == s.id
        ).count()
        s.imported_count = session.query(Video).filter(
            Video.user_id == user.id, Video.source_id == s.id, Video.status.in_(["imported", "processed"])
        ).count()
    return rows


@router.post("/sources", response_model=SourceOut)
def api_create_source(body: SourceCreate, session: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    allowed = {"drive_folder", "video_url", "channel", "playlist", "webpage"}
    if body.type not in allowed:
        raise HTTPException(400, f"Unsupported source type: {body.type}")
    name = body.name or (body.url_or_id.rstrip("/").rsplit("/", 1)[-1] or "source")
    source = Source(
        user_id=user.id, type=body.type, name=name,
        url_or_id=body.url_or_id, folder_id=body.folder_id,
        config=body.config, monitoring=True,
    )
    session.add(source)
    session.flush()
    return source


@router.delete("/sources/{source_id}")
def api_delete_source(source_id: int, session: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    source = session.get(Source, source_id)
    if not source or source.user_id != user.id:
        raise HTTPException(404, "Source not found")
    session.query(Video).filter(Video.source_id == source_id, Video.user_id == user.id).delete()
    session.delete(source)
    session.flush()
    return {"ok": True}


@router.post("/sources/{source_id}/scan")
def api_scan_source(source_id: int, session: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """Scan a source and register discovered videos."""
    source = session.get(Source, source_id)
    if not source or source.user_id != user.id:
        raise HTTPException(404, "Source not found")
    source.scan_status = "running"
    source.scan_error = ""
    session.flush()
    try:
        discovered = 0
        if source.type == "drive_folder":
            conn = session.query(DriveConnection).filter(
                DriveConnection.user_id == user.id, DriveConnection.status == "connected"
            ).first()
            if not conn:
                raise HTTPException(400, "Google Drive is not connected")
            client, _ = drive_google.client_from_connection(conn)
            videos, _stats = drive_google.scan_folders(client, [source.url_or_id])
            discovered = _register_drive_videos(session, user.id, conn.id, videos, source)
        elif source.type in ("video_url", "channel", "playlist", "webpage"):
            url = source.url_or_id
            if source.type in ("channel", "playlist"):
                info = source_scanner.ytdlp_extract_info(url)
                items = info.get("videos", [])
                if source.type == "playlist" and info.get("title") and (
                    not source.name or source.name == "source"
                ):
                    source.name = info["title"][:255]
            else:
                result = (
                    source_scanner.scan_video_url(url)
                    if source.type == "video_url"
                    else source_scanner.scan_webpage(url)
                )
                items = result.videos
            discovered = _register_url_videos(session, user.id, source, items)
        source.discovered_count = discovered
        source.last_scanned_at = utcnow()
        source.scan_status = "ok"
    except HTTPException:
        raise
    except Exception as exc:
        source.scan_status = "error"
        source.scan_error = str(exc)[:2000]
        raise HTTPException(500, source.scan_error)
    session.flush()
    return {"ok": True, "discovered": discovered}


def _register_drive_videos(session: Session, user_id: int, conn_id: int,
                           videos: list[dict], source: Source) -> int:
    count = 0
    for v in videos:
        df = session.query(DriveFile).filter(
            DriveFile.user_id == user_id, DriveFile.drive_file_id == v["id"]
        ).first()
        if not df:
            df = DriveFile(
                user_id=user_id, connection_id=conn_id, drive_file_id=v["id"],
                name=v["name"], mime_type=v["mime_type"], size=v["size"],
                modified_at=v["modified_at"], parent_id=v.get("parent_id", ""),
                relative_path=v.get("relative_path", ""), is_shortcut=v.get("is_shortcut", False),
                downloadable=v.get("downloadable", True), checksum=v.get("checksum", ""),
                version=v.get("version", ""), import_status="discovered",
            )
            session.add(df)
            session.flush()
        # only create the Video row once; update import status freshness
        vid = session.query(Video).filter(
            Video.user_id == user_id, Video.drive_file_id == df.id
        ).first()
        if not vid:
            session.add(Video(
                user_id=user_id, source_id=source.id, drive_file_id=df.id, kind="drive",
                name=v["name"], folder_path=v.get("relative_path", ""),
                status="discovered",
            ))
        count += 1
    session.flush()
    return count


def _register_url_videos(session: Session, user_id: int, source: Source,
                         items: list[dict]) -> int:
    count = 0
    for item in items:
        url = item.get("url", "")
        if not url:
            continue
        existing = session.query(Video).filter(
            Video.user_id == user_id, Video.origin_url == url
        ).first()
        if existing:
            continue
        session.add(Video(
            user_id=user_id, source_id=source.id, kind="url" if source.type == "video_url" else "page",
            name=item.get("name", "") or "video", origin_url=url,
            folder_path=source.name, status="discovered",
        ))
        count += 1
    session.flush()
    return count


@router.post("/sources/{source_id}/monitor")
def api_toggle_monitor(source_id: int, monitoring: bool, session: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    source = session.get(Source, source_id)
    if not source or source.user_id != user.id:
        raise HTTPException(404, "Source not found")
    source.monitoring = monitoring
    session.flush()
    return {"ok": True, "monitoring": source.monitoring}


# ---------------------------------------------------------------- google drive
@router.get("/drive/status", response_model=DriveConnectStatus)
def api_drive_status(session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    conn = session.query(DriveConnection).filter(DriveConnection.user_id == user.id).first()
    if not conn:
        return DriveConnectStatus(connected=False, status="disconnected")
    return DriveConnectStatus(connected=conn.status == "connected",
                              account_email=conn.account_email, status=conn.status)


@router.get("/drive/auth/start")
def api_drive_auth_start(session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if not drive_google.oauth_configured():
        raise HTTPException(400, "Google OAuth is not configured (set ARCUS_GOOGLE_OAUTH_CLIENT_ID/SECRET)")
    from ..models import OAuthState

    state = secrets.token_urlsafe(24)
    session.add(OAuthState(user_id=user.id, state=state, created_at=utcnow(), used=False))
    session.flush()
    return {"auth_url": drive_google.build_auth_url(state)}


@router.get("/drive/auth/callback")
def api_drive_auth_callback(code: str, state: str, session: Session = Depends(get_db),
                            user: User = Depends(get_current_user)):
    from ..models import OAuthState

    state_row = session.query(OAuthState).filter(OAuthState.state == state, OAuthState.user_id == user.id).first()
    if not state_row:
        raise HTTPException(400, "Invalid or unknown OAuth state")
    if state_row.used:
        raise HTTPException(400, "OAuth state already used")
    if not drive_google.oauth_configured():
        raise HTTPException(400, "Google OAuth is not configured")
    tokens = drive_google.exchange_code(code)
    conn = session.query(DriveConnection).filter(DriveConnection.user_id == user.id).first()
    if not conn:
        conn = DriveConnection(user_id=user.id, status="connected")
        session.add(conn)
    drive_google.store_tokens(conn, tokens)
    try:
        client, about = drive_google.client_from_connection(conn)
        conn.account_email = about.get("email", "")
        conn.status = "connected"
        conn.last_verified_at = utcnow()
    except Exception as exc:
        conn.status = "expired"
        raise HTTPException(502, f"Drive verification failed: {exc}")
    state_row.used = True
    session.flush()
    return {"ok": True, "account_email": conn.account_email}


@router.post("/drive/disconnect")
def api_drive_disconnect(session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    conn = session.query(DriveConnection).filter(DriveConnection.user_id == user.id).first()
    if conn:
        conn.status = "disconnected"
        session.flush()
    return {"ok": True}


@router.get("/drive/roots", response_model=list[DriveFolderNode])
def api_drive_roots(session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    conn = session.query(DriveConnection).filter(
        DriveConnection.user_id == user.id, DriveConnection.status == "connected"
    ).first()
    if not conn:
        raise HTTPException(400, "Google Drive is not connected")
    client, _ = drive_google.client_from_connection(conn)
    return drive_google.list_roots(client)


@router.get("/drive/folders/{folder_id}")
def api_drive_folder(folder_id: str, session: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    conn = session.query(DriveConnection).filter(
        DriveConnection.user_id == user.id, DriveConnection.status == "connected"
    ).first()
    if not conn:
        raise HTTPException(400, "Google Drive is not connected")
    client, _ = drive_google.client_from_connection(conn)
    children = drive_google.list_children(client, folder_id)
    folders = [
        {"id": c["id"], "name": c["name"], "kind": "folder", "is_root": False}
        for c in children if c["mime_type"] == "application/vnd.google-apps.folder"
    ]
    files = [
        {"id": c["id"], "name": c["name"], "mime_type": c["mime_type"], "size": c["size"],
         "modified_at": c["modified_at"].isoformat() if c["modified_at"] else ""}
        for c in children
        if drive_google.is_video_file(c["mime_type"], c["name"])
    ]
    return {"folders": folders, "video_files": files}


@router.post("/drive/scan")
def api_drive_scan(body: DriveScanRequest, session: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    conn = session.query(DriveConnection).filter(
        DriveConnection.user_id == user.id, DriveConnection.status == "connected"
    ).first()
    if not conn:
        raise HTTPException(400, "Google Drive is not connected")
    client, _ = drive_google.client_from_connection(conn)
    videos, stats = drive_google.scan_folders(client, body.folder_ids, body.exclude_ids)
    _register_drive_videos(session, user.id, conn.id, videos, None)
    source = Source(user_id=user.id, type="drive_folder", name="Drive scan",
                    url_or_id=",".join(body.folder_ids), folder_id=str(conn.id),
                    scan_status="ok", last_scanned_at=utcnow(), discovered_count=len(videos))
    session.add(source)
    session.flush()
    return {"ok": True, "videos": len(videos), "stats": stats}


# ---------------------------------------------------------------- videos
@router.get("/videos", response_model=list[VideoOut])
def api_list_videos(source_id: int | None = None, status: str | None = None,
                    session: Session = Depends(get_db), user: User = Depends(get_current_user)):
    q = session.query(Video).filter(Video.user_id == user.id)
    if source_id:
        q = q.filter(Video.source_id == source_id)
    if status:
        q = q.filter(Video.status == status)
    return q.order_by(Video.created_at.desc()).all()


@router.post("/videos/upload", response_model=VideoOut)
def api_upload_video(file: UploadFile, session: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    name = safe_filename(file.filename or "upload")
    suffix = Path(name).suffix.lower().lstrip(".")
    if suffix not in settings.supported_video_suffixes:
        raise HTTPException(400, f"Unsupported format: {suffix}")
    dest = settings.video_path / "uploads"
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / name
    with open(path, "wb") as fh:
        while chunk := file.file.read(1 << 20):
            fh.write(chunk)
    info = ffmpeg_engine.probe(str(path))
    if not info.valid:
        path.unlink(missing_ok=True)
        raise HTTPException(400, f"File is not a valid video: {info.detail}")
    video = Video(
        user_id=user.id, kind="upload", name=Path(name).stem, local_path=str(path),
        duration=info.duration, width=info.width, height=info.height, size=info.size,
        has_audio=info.has_audio, media_valid=True, status="imported",
        imported_at=utcnow(), status_detail="Uploaded via Web UI",
    )
    session.add(video)
    session.flush()
    return video


@router.get("/videos/{video_id}/transcript", response_model=TranscriptOut)
def api_transcript(video_id: int, session: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    video = session.get(Video, video_id)
    if not video or video.user_id != user.id:
        raise HTTPException(404, "Video not found")
    t = session.query(Transcript).filter(Transcript.video_id == video_id).first()
    if not t:
        raise HTTPException(404, "No transcript yet — run a batch or transcribe first")
    return t


@router.post("/videos/{video_id}/transcribe")
def api_transcribe(video_id: int, session: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    video = session.get(Video, video_id)
    if not video or video.user_id != user.id:
        raise HTTPException(404, "Video not found")
    if not video.local_path or not Path(video.local_path).exists():
        raise HTTPException(400, "Video file is not available locally")
    job = Job(user_id=user.id, video_id=video.id, kind="transcribe", status="running",
              stage="transcription")
    session.add(job)
    session.flush()
    try:
        media = ffmpeg_engine.probe(video.local_path)
        source_hash = whisper_service.file_hash(video.local_path)
        wav_path = str(settings.transcript_path / f"{video.id}.wav")
        if media.has_audio:
            result = whisper_service.transcribe_video_file(video.local_path, wav_path)
        else:
            result = {"language": "unknown", "duration": media.duration,
                      "segments": [{"start": 0.0, "end": media.duration, "text": "", "words": [], "word_count": 0}],
                      "model": "silence"}
        whisper_service.persist_transcript(video.id, user.id, result, source_hash, session)
        job.status = "done"
        job.stage = "done"
        job.progress = 1.0
    except Exception as exc:
        job.status = "failed"
        job.error = str(exc)[:2000]
        session.flush()
        raise HTTPException(500, str(exc))
    session.flush()
    return {"ok": True}
