"""SQLAlchemy ORM models for ArcusClips.

Every user-owned table carries ``user_id`` for per-user isolation.
All timestamps are naive UTC. JSON payloads use the portable SQLAlchemy JSON type.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, utcnow


# ---------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SessionToken(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class AppSetting(Base):
    """Key/value settings store (AI settings, Whisper, render defaults, branding…)."""
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True, nullable=True)
    value: Mapped[Any] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------
class DriveConnection(Base):
    __tablename__ = "drive_connections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    account_email: Mapped[str] = mapped_column(String(255), default="")
    encrypted_access_token: Mapped[str] = mapped_column(Text, default="")
    encrypted_refresh_token: Mapped[str] = mapped_column(Text, default="")
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="connected")  # connected|disconnected|expired
    connected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Source(Base):
    """A configurable content source: Drive folder, URL, channel, playlist, webpage."""
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    type: Mapped[str] = mapped_column(String(32))  # drive_folder|video_url|channel|playlist|webpage
    name: Mapped[str] = mapped_column(String(255))
    url_or_id: Mapped[str] = mapped_column(String(1024))  # Drive folder id or URL
    folder_id: Mapped[str] = mapped_column(String(255), default="")  # drive connection root
    config: Mapped[Any] = mapped_column(JSON, default=dict)  # crawl depth, include/exclude, etc.
    monitoring: Mapped[bool] = mapped_column(Boolean, default=True)
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    scan_status: Mapped[str] = mapped_column(String(32), default="pending")  # pending|running|ok|error
    scan_error: Mapped[str] = mapped_column(Text, default="")
    discovered_count: Mapped[int] = mapped_column(Integer, default=0)
    imported_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    videos: Mapped[list["Video"]] = relationship(back_populates="source")


class DriveFile(Base):
    """An inventory entry for a discovered Google Drive file."""
    __tablename__ = "drive_files"
    __table_args__ = (
        UniqueConstraint("user_id", "drive_file_id", name="uq_drive_file_per_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    connection_id: Mapped[int | None] = mapped_column(ForeignKey("drive_connections.id"), nullable=True)
    drive_file_id: Mapped[str] = mapped_column(String(255), index=True)
    target_id: Mapped[str] = mapped_column(String(255), default="")
    drive_id: Mapped[str] = mapped_column(String(255), default="")
    name: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(128), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    modified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    parent_id: Mapped[str] = mapped_column(String(255), default="")
    relative_path: Mapped[str] = mapped_column(String(1024), default="")
    is_shortcut: Mapped[bool] = mapped_column(Boolean, default=False)
    downloadable: Mapped[bool] = mapped_column(Boolean, default=True)
    checksum: Mapped[str] = mapped_column(String(128), default="")
    version: Mapped[str] = mapped_column(String(64), default="")  # Drive etag/version for skip-unchanged
    import_status: Mapped[str] = mapped_column(String(32), default="discovered")  # discovered|imported|failed|skipped
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    video: Mapped["Video | None"] = relationship(back_populates="drive_file", uselist=False)


class Video(Base):
    """A single discovered/imported video eligible for clipping."""
    __tablename__ = "videos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), index=True, nullable=True)
    drive_file_id: Mapped[int | None] = mapped_column(ForeignKey("drive_files.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), default="drive")  # drive|url|page|upload
    name: Mapped[str] = mapped_column(String(255))
    origin_url: Mapped[str] = mapped_column(String(2048), default="")
    folder_path: Mapped[str] = mapped_column(String(1024), default="")  # original Drive/page context
    local_path: Mapped[str] = mapped_column(String(2048), default="")
    duration: Mapped[float] = mapped_column(Float, default=0.0)
    size: Mapped[int] = mapped_column(Integer, default=0)
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    has_audio: Mapped[bool] = mapped_column(Boolean, default=True)
    media_valid: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="discovered")  # discovered|imported|processed|failed
    status_detail: Mapped[str] = mapped_column(Text, default="")
    imported_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    source: Mapped["Source | None"] = relationship(back_populates="videos")
    drive_file: Mapped["DriveFile | None"] = relationship(back_populates="video")
    transcript: Mapped["Transcript | None"] = relationship(
        back_populates="video", uselist=False, cascade="all, delete-orphan"
    )
    jobs: Mapped[list["Job"]] = relationship(back_populates="video")
    clips: Mapped[list["Clip"]] = relationship(back_populates="video")


class Transcript(Base):
    """Persistent Whisper transcript with timestamped segments."""
    __tablename__ = "transcripts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id"), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    language: Mapped[str] = mapped_column(String(32), default="")
    model_name: Mapped[str] = mapped_column(String(64), default="")
    duration: Mapped[float] = mapped_column(Float, default=0.0)
    segments: Mapped[Any] = mapped_column(JSON, default=list)  # [{start,end,text,...}]
    source_hash: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    video: Mapped["Video"] = relationship(back_populates="transcript")


# ---------------------------------------------------------------------------
# Batch jobs & workers
# ---------------------------------------------------------------------------
class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(32), default="queued")
    # queued|scanning|downloading|transcribing|analyzing|selecting|rendering|packaging|completed|partial|failed|cancelled
    options: Mapped[Any] = mapped_column(JSON, default=dict)
    total_videos: Mapped[int] = mapped_column(Integer, default=0)
    completed_videos: Mapped[int] = mapped_column(Integer, default=0)
    failed_videos: Mapped[int] = mapped_column(Integer, default=0)
    clip_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    jobs: Mapped[list["Job"]] = relationship(back_populates="batch")


class Job(Base):
    """A persistent, recoverable processing job.

    One job per source video inside a batch (plus scanning/import jobs).
    Stages: pending|importing|ffprobe|transcription|analysis|selection|rendering|done|failed|cancelled
    """
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"), index=True, nullable=True)
    video_id: Mapped[int | None] = mapped_column(ForeignKey("videos.id"), index=True, nullable=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), default="render")  # scan|import|transcribe|analyze|render|package
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending|running|done|failed|cancelled|paused
    stage: Mapped[str] = mapped_column(String(64), default="queued")
    progress: Mapped[float] = mapped_column(Float, default=0.0)  # 0..1
    payload: Mapped[Any] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    worker: Mapped[str] = mapped_column(String(64), default="")
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    batch: Mapped["Batch | None"] = relationship(back_populates="jobs")
    video: Mapped["Video | None"] = relationship(back_populates="jobs")


# ---------------------------------------------------------------------------
# Clips & render versions
# ---------------------------------------------------------------------------
class Clip(Base):
    __tablename__ = "clips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"), index=True, nullable=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id"), index=True)
    index: Mapped[int] = mapped_column(Integer, default=0)
    start_time: Mapped[float] = mapped_column(Float)
    end_time: Mapped[float] = mapped_column(Float)
    hook: Mapped[str] = mapped_column(Text, default="")
    title: Mapped[str] = mapped_column(String(512), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    score: Mapped[float] = mapped_column(Float, default=0.0)
    category: Mapped[str] = mapped_column(String(64), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    caption_style: Mapped[str] = mapped_column(String(32), default="bounce")
    layout: Mapped[str] = mapped_column(String(32), default="blur")  # full|blur|zoom
    handle: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(32), default="selected")  # selected|rendered|rendering|failed|edited
    is_heuristic: Mapped[bool] = mapped_column(Boolean, default=False)
    current_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    video: Mapped["Video"] = relationship(back_populates="clips")
    versions: Mapped[list["ClipVersion"]] = relationship(
        back_populates="clip", cascade="all, delete-orphan", order_by="ClipVersion.version"
    )


class ClipVersion(Base):
    """One rendered output of a clip. Previous successful versions are preserved."""
    __tablename__ = "clip_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    clip_id: Mapped[int] = mapped_column(ForeignKey("clips.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    render_path: Mapped[str] = mapped_column(String(2048), default="")
    thumbnail_path: Mapped[str] = mapped_column(String(2048), default="")
    duration: Mapped[float] = mapped_column(Float, default=0.0)
    size: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="rendered")  # rendering|rendered|failed
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    clip: Mapped["Clip"] = relationship(back_populates="versions")


class Export(Base):
    """A generated ZIP package for a batch."""
    __tablename__ = "exports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)
    zip_path: Mapped[str] = mapped_column(String(2048), default="")
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending|building|done|failed
    size: Mapped[int] = mapped_column(Integer, default=0)
    clip_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OAuthState(Base):
    """One-time OAuth state tokens for the Google Drive authorization flow."""
    __tablename__ = "oauth_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    state: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    used: Mapped[bool] = mapped_column(Boolean, default=False)


class Event(Base):
    """Application event log (errors, lifecycle milestones) for the log viewer."""
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True, nullable=True)
    level: Mapped[str] = mapped_column(String(16), default="info")
    source: Mapped[str] = mapped_column(String(64), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
