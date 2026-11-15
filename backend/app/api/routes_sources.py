"""Source + Drive + video routes."""
from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
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
def api_scan_source(source_id: int, drive_id: str | None = Query(default=None),
                    session: Session = Depends(get_db),
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
            effective_drive_id = drive_id
            if effective_drive_id is None:
                effective_drive_id = ((source.config or {}).get("drive_id") or "").strip() or None
            videos, _stats = drive_google.scan_folders(
                client, [source.url_or_id], drive_id=effective_drive_id
            )
            if drive_id is not None:
                source.config = {**(source.config or {}), "drive_id": drive_id}
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
    """Create-or-update inventory entries for discovered Drive videos.

    Identity semantics:
      * normal file: ``drive_file_id == target_id ==`` the file's own ID;
      * shortcut: ``drive_file_id`` keeps the shortcut ID (identity/display/
        dedup) and ``target_id`` holds the resolved target file ID used for
        media downloads (never the shortcut ID).

    Rescanning refreshes all provider-supplied metadata (target_id, drive_id,
    name, mime_type, size, modified_at, parent_id, relative_path, is_shortcut,
    downloadable, checksum, version), so records created before the target_id /
    drive_id migration are repaired in place. Existing ``Video`` rows keep
    their identity and associations — nothing is duplicated or rebound.
    """
    count = 0
    source_id = source.id if source is not None else None
    for v in videos:
        drive_file_id = v["id"]
        is_shortcut = bool(v.get("is_shortcut", False))
        incoming_target = (v.get("target_id") or "").strip()
        if not is_shortcut:
            # Normal files always use their own ID as the media target.
            target_id = drive_file_id
        else:
            target_id = incoming_target  # may be "" (unresolved) — repaired on rescan

        provider = {
            "target_id": target_id,
            "drive_id": v.get("drive_id") or "",
            "name": v["name"],
            "mime_type": v["mime_type"],
            "size": v["size"],
            "modified_at": v["modified_at"],
            "parent_id": v.get("parent_id", ""),
            "relative_path": v.get("relative_path", ""),
            "is_shortcut": is_shortcut,
            "downloadable": v.get("downloadable", True),
            "checksum": v.get("checksum", ""),
            "version": v.get("version", ""),
        }

        df = session.query(DriveFile).filter(
            DriveFile.user_id == user_id, DriveFile.drive_file_id == drive_file_id
        ).first()
        if df is None:
            df = DriveFile(
                user_id=user_id, connection_id=conn_id, drive_file_id=drive_file_id,
                import_status="discovered", **provider,
            )
            session.add(df)
            session.flush()
        else:
            if provider["is_shortcut"] and not provider["target_id"] and df.target_id:
                # Never wipe a known target with an unresolved one.
                provider.pop("target_id")
            for key, value in provider.items():
                setattr(df, key, value)
            session.flush()

        # only create the Video row once; keep associations and metadata stable
        vid = session.query(Video).filter(
            Video.user_id == user_id, Video.drive_file_id == df.id
        ).first()
        if vid is None:
            session.add(Video(
                user_id=user_id, source_id=source_id, drive_file_id=df.id, kind="drive",
                name=v["name"], folder_path=v.get("relative_path", ""),
                status="discovered",
            ))
        else:
            # Refresh display metadata without touching processing state, and
            # backfill a missing source link instead of rebinding it.
            vid.name = v["name"]
            vid.folder_path = v.get("relative_path", "")
            if vid.source_id is None and source_id is not None:
                vid.source_id = source_id
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
def api_drive_auth_callback(code: str | None = None, state: str | None = None,
                            error: str | None = None, session: Session = Depends(get_db)):
    """Receive Google's OAuth redirect.

    This is a browser redirect and does NOT carry the app's bearer token, so it
    must not depend on ``get_current_user``. The initiating user is resolved
    from the validated, one-time OAuth state record — never from a query param.
    """
    from ..models import OAuthState

    if error:
        raise HTTPException(400, f"Google Drive authorization was denied: {error}")
    if not state:
        raise HTTPException(400, "Missing OAuth state")
    if not code:
        raise HTTPException(400, "Missing authorization code")

    state_row = session.query(OAuthState).filter(OAuthState.state == state).first()
    if not state_row:
        raise HTTPException(400, "Invalid or unknown OAuth state")
    # enforce a short expiry window before consuming
    age = (utcnow() - state_row.created_at).total_seconds()
    if age > settings.oauth_state_ttl_seconds:
        # mark expired so it cannot be reused
        session.query(OAuthState).filter(
            OAuthState.id == state_row.id, OAuthState.used == False  # noqa: E712
        ).update({"used": True})
        session.flush()
        raise HTTPException(400, "OAuth state has expired")

    # Atomically consume the state with a conditional UPDATE so two concurrent
    # callbacks cannot both consume the same state. The rowcount tells us if
    # this callback won the race. Consumption is committed in its own
    # transaction BEFORE exchanging the authorization code, so a failed
    # exchange still burns the state (it cannot be replayed).
    from ..db import get_session_factory

    consume_session = get_session_factory()()
    try:
        consumed = (
            consume_session.query(OAuthState)
            .filter(OAuthState.id == state_row.id, OAuthState.used == False)  # noqa: E712
            .update({"used": True}, synchronize_session=False)
        )
        consume_session.commit()
    finally:
        consume_session.close()
    if not consumed:
        raise HTTPException(400, "OAuth state already used")

    user = session.get(User, state_row.user_id)
    if not user:
        raise HTTPException(400, "OAuth state references a missing user")
    if not drive_google.oauth_configured():
        raise HTTPException(400, "Google OAuth is not configured")
    try:
        tokens = drive_google.exchange_code(code)
    except Exception as exc:
        raise HTTPException(502, f"Token exchange failed: {exc}")
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
        session.flush()
        raise HTTPException(502, f"Drive verification failed: {exc}")
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
    try:
        return drive_google.list_roots(client)
    except drive_google.DriveError as exc:
        # Never present a partial Shared Drive listing as if it were complete.
        raise HTTPException(502, str(exc))


@router.get("/drive/folders/{folder_id}")
def api_drive_folder(folder_id: str, drive_id: str | None = Query(default=None),
                     session: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    conn = session.query(DriveConnection).filter(
        DriveConnection.user_id == user.id, DriveConnection.status == "connected"
    ).first()
    if not conn:
        raise HTTPException(400, "Google Drive is not connected")
    client, _ = drive_google.client_from_connection(conn)
    children = drive_google.list_children(client, folder_id, drive_id=drive_id)
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
    # Propagate the shared-drive ID so folder listing, shortcut resolution and
    # downloads are scoped to the correct Shared Drive.
    videos, stats = drive_google.scan_folders(
        client, body.folder_ids, body.exclude_ids, drive_id=body.drive_id
    )

    folder_key = ",".join(body.folder_ids)
    # Reuse the existing Source for the same user + folder set (and the same
    # drive context) so repeated scans do not create duplicate Source records.
    # JSON config cannot be filtered portably in SQL — match in Python.
    candidates = session.query(Source).filter(
        Source.user_id == user.id,
        Source.type == "drive_folder",
        Source.url_or_id == folder_key,
    ).all()
    wanted_drive = body.drive_id or ""
    source = next(
        (c for c in candidates if ((c.config or {}).get("drive_id") or "") == wanted_drive),
        None,
    )
    if source is None:
        source = Source(user_id=user.id, type="drive_folder", name="Drive scan",
                        url_or_id=folder_key, folder_id=str(conn.id))
        session.add(source)
        session.flush()
    # record the drive context for future scans
    source.config = {**(source.config or {}), "drive_id": body.drive_id or ""}
    source.scan_status = "ok"
    source.scan_error = ""
    source.last_scanned_at = utcnow()
    session.flush()
    registered = _register_drive_videos(session, user.id, conn.id, videos, source)
    source.discovered_count = registered
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
