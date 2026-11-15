"""Pydantic request/response schemas for the API."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- auth
class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    token: str
    user_id: int
    username: str
    is_admin: bool


class UserOut(BaseModel):
    id: int
    username: str
    is_admin: bool

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------- settings
class LMStudioSettings(BaseModel):
    base_url: str = "http://127.0.0.1:1234/v1"
    model: str = ""
    api_key: str = ""  # stored server-side only; never returned to the frontend
    request_timeout: float = 120.0
    max_tokens: int = 2048
    temperature: float = 0.4
    max_context_tokens: int = 8192


class WhisperSettings(BaseModel):
    model: str = "base"
    model_dir: str = ""
    device: str = "auto"
    compute_type: str = "auto"
    language: str = ""


class RenderSettings(BaseModel):
    width: int = 1080
    height: int = 1920
    fps: int = 30
    crf: int = 20
    preset: str = "medium"


class ProcessingSettings(BaseModel):
    max_processing_concurrency: int = 2
    max_render_concurrency: int = 2
    max_download_concurrency: int = 2
    allow_heuristic_fallback: bool = True


class BrandingSettings(BaseModel):
    creator_handle: str = ""
    default_caption_style: str = "bounce"
    default_layout: str = "blur"
    font_name: str = ""


class SettingsUpdate(BaseModel):
    lmstudio: Optional[LMStudioSettings] = None
    whisper: Optional[WhisperSettings] = None
    render: Optional[RenderSettings] = None
    processing: Optional[ProcessingSettings] = None
    branding: Optional[BrandingSettings] = None


class LMStudioModelsResponse(BaseModel):
    connected: bool
    models: list[str] = []
    error: str = ""


# ---------------------------------------------------------------- sources
class SourceCreate(BaseModel):
    type: str  # drive_folder|video_url|channel|playlist|webpage
    name: str = ""
    url_or_id: str
    folder_id: str = ""
    config: dict[str, Any] = Field(default_factory=dict)


class SourceOut(BaseModel):
    id: int
    type: str
    name: str
    url_or_id: str
    folder_id: str
    config: dict[str, Any]
    monitoring: bool
    last_scanned_at: Optional[datetime] = None
    scan_status: str
    scan_error: str
    discovered_count: int
    imported_count: int
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------- drive
class DriveConnectStatus(BaseModel):
    connected: bool
    account_email: str = ""
    status: str = "disconnected"


class DriveFolderNode(BaseModel):
    id: str
    name: str
    kind: str  # folder|drive
    is_root: bool = False
    file_count: int = 0
    folder_count: int = 0


class DriveScanRequest(BaseModel):
    folder_ids: list[str] = Field(min_length=1)
    exclude_ids: list[str] = Field(default_factory=list)
    drive_id: str | None = None  # Shared Drive ID, when scanning a shared drive


class DriveScanProgress(BaseModel):
    total: int
    visited: int
    files: int
    done: bool
    current: str = ""


# ---------------------------------------------------------------- batches
class ClipQuantity(BaseModel):
    mode: str = "best"  # best|custom
    per_video: int = 3  # used when mode=best
    custom_total: int = 0  # used when mode=custom


class DurationRange(BaseModel):
    min_seconds: float = 15.0
    max_seconds: float = 30.0


class BatchOptions(BaseModel):
    name: str = ""
    clips_per_video: int = 3
    max_total_clips: int = 0
    duration: DurationRange = Field(default_factory=DurationRange)
    quality: str = "balanced"  # strict|balanced|max_coverage
    caption_style: str = "bounce"
    layout: str = "blur"
    creator_handle: str = ""
    skip_processed: bool = True
    continue_on_error: bool = True
    source_ids: list[int] = Field(default_factory=list)
    video_ids: list[int] = Field(default_factory=list)


class BatchCreateRequest(BatchOptions):
    pass


class BatchOut(BaseModel):
    id: int
    name: str
    status: str
    options: dict[str, Any]
    total_videos: int
    completed_videos: int
    failed_videos: int
    clip_count: int
    error: str
    created_at: datetime
    finished_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class JobOut(BaseModel):
    id: int
    batch_id: Optional[int] = None
    video_id: Optional[int] = None
    kind: str
    status: str
    stage: str
    progress: float
    error: str
    retry_count: int
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------- videos / clips
class VideoOut(BaseModel):
    id: int
    source_id: Optional[int] = None
    kind: str
    name: str
    origin_url: str
    folder_path: str
    local_path: str
    duration: float
    size: int
    width: int
    height: int
    has_audio: bool
    media_valid: bool
    status: str
    status_detail: str
    created_at: datetime

    model_config = {"from_attributes": True}


class TranscriptOut(BaseModel):
    id: int
    video_id: int
    language: str
    model_name: str
    duration: float
    segments: list[dict[str, Any]]
    created_at: datetime

    model_config = {"from_attributes": True}


class ClipUpdate(BaseModel):
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    hook: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    caption_style: Optional[str] = None
    layout: Optional[str] = None
    handle: Optional[str] = None
    category: Optional[str] = None


class ClipVersionOut(BaseModel):
    id: int
    version: int
    render_path: str
    thumbnail_path: str
    duration: float
    size: int
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ClipOut(BaseModel):
    id: int
    batch_id: Optional[int] = None
    video_id: int
    video_name: str = ""
    folder_path: str = ""
    index: int
    start_time: float
    end_time: float
    duration: float = 0.0
    hook: str
    title: str
    description: str
    score: float
    category: str
    reason: str
    caption_style: str
    layout: str
    handle: str
    status: str
    is_heuristic: bool
    current_version: int
    versions: list[ClipVersionOut] = Field(default_factory=list)
    created_at: datetime

    model_config = {"from_attributes": True}


class ClipCreatedOut(BaseModel):
    id: int
    status: str
    message: str


# ---------------------------------------------------------------- exports
class ExportOut(BaseModel):
    id: int
    batch_id: int
    zip_path: str
    status: str
    size: int
    clip_count: int
    error: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------- dashboard
class DashboardStats(BaseModel):
    sources_connected: int
    discovered_videos: int
    imported_videos: int
    processed_videos: int
    clips_generated: int
    active_batches: int
    failed_jobs: int
    storage_bytes: int
    storage_limit: int
    workers_available: bool
    lmstudio_connected: bool


class WorkerStatus(BaseModel):
    running: bool
    active_jobs: int
    capacity: int
    poll_interval: float
