"""Application configuration.

All settings are driven by environment variables / a local ``.env`` file
(see ``backend/.env.example``). Every important path is configurable so the
app can run natively on Windows or Linux without Docker.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_data_dir() -> str:
    # backend/.. = repository root; data lives in <repo>/data
    return str(Path(__file__).resolve().parent.parent.parent / "data")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="ARCUS_",
        extra="ignore",
    )

    # ---- Application ----
    app_name: str = "ArcusClips"
    environment: str = "development"
    secret_key: str = "change-me-in-production-local-dev-key"
    host: str = "127.0.0.1"
    port: int = 8000

    # ---- Database ----
    database_url: str = ""  # defaults to sqlite under data_dir

    # ---- Storage ----
    data_dir: str = _default_data_dir()
    # Storage limits (bytes)
    max_storage_bytes: int = 200 * 1024**3  # 200 GB
    video_subdir: str = "videos"
    transcript_subdir: str = "transcripts"
    render_subdir: str = "renders"
    export_subdir: str = "exports"
    job_subdir: str = "jobs"

    # ---- Render defaults ----
    render_width: int = 1080
    render_height: int = 1920
    render_fps: int = 30
    render_crf: int = 20
    render_preset: str = "medium"

    # ---- FFmpeg ----
    # Empty string => auto-detect via imageio-ffmpeg, then PATH.
    ffmpeg_path: str = ""
    ffprobe_path: str = ""

    # ---- Whisper (faster-whisper, local) ----
    whisper_model: str = "base"
    whisper_model_dir: str = ""  # optional local model directory / HF cache override
    whisper_device: str = "auto"  # auto | cpu | cuda
    whisper_compute_type: str = "auto"
    whisper_language: str = ""  # empty = auto detect
    whisper_max_speakers: int = 1

    # ---- LM Studio (local OpenAI-compatible server) ----
    lmstudio_base_url: str = "http://127.0.0.1:1234/v1"
    lmstudio_model: str = ""  # empty = auto-detect from /v1/models
    lmstudio_api_key: str = ""  # only required if the local server demands it
    lmstudio_timeout: float = 120.0
    lmstudio_max_tokens: int = 2048
    lmstudio_temperature: float = 0.4
    lmstudio_max_context_tokens: int = 8192
    # When LM Studio is unavailable, fall back to deterministic local heuristics.
    allow_heuristic_fallback: bool = True

    # ---- Processing resources ----
    max_processing_concurrency: int = 2
    max_render_concurrency: int = 2
    max_download_concurrency: int = 2
    job_poll_interval: float = 1.0
    job_stale_timeout: float = 1800.0  # 30 min without heartbeat => recoverable

    # ---- Redis (optional; DB queue is the default) ----
    redis_url: str = ""

    # ---- Google Drive OAuth ----
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_oauth_redirect_uri: str = "http://127.0.0.1:8000/api/auth/google/callback"
    google_drive_scope: str = "https://www.googleapis.com/auth/drive.readonly"
    oauth_credential_encryption_key: str = ""  # Fernet key; auto-generated if empty

    # ---- Source scanning ----
    allowed_source_domains: str = ""  # comma separated; empty = no domain restriction
    crawl_max_depth: int = 2
    crawl_max_pages: int = 25
    crawl_rate_limit_delay: float = 0.5
    max_download_size: int = 4 * 1024**3  # 4 GB per source file
    supported_video_formats: str = (
        "mp4,mov,mkv,webm,m4v,avi,mpeg,mpg,ts"
    )

    # ---- Auth ----
    auth_enabled: bool = True
    default_admin_username: str = "admin"
    default_admin_password: str = "changeme"
    session_ttl_hours: int = 72

    # ---- Rate limiting ----
    rate_limit_requests_per_minute: int = 600

    # -----------------------------------------------------------------
    @property
    def data_path(self) -> Path:
        return Path(self.data_dir).expanduser().resolve()

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.data_path / 'arcusclips.db'}"

    @property
    def video_path(self) -> Path:
        return self.data_path / self.video_subdir

    @property
    def transcript_path(self) -> Path:
        return self.data_path / self.transcript_subdir

    @property
    def render_path(self) -> Path:
        return self.data_path / self.render_subdir

    @property
    def export_path(self) -> Path:
        return self.data_path / self.export_subdir

    @property
    def job_path(self) -> Path:
        return self.data_path / self.job_subdir

    @property
    def supported_video_suffixes(self) -> set[str]:
        return {s.strip().lower().lstrip(".") for s in self.supported_video_formats.split(",") if s.strip()}

    def ensure_dirs(self) -> None:
        for p in (
            self.data_path, self.video_path, self.transcript_path,
            self.render_path, self.export_path, self.job_path,
        ):
            p.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()


class _SettingsProxy:
    """Module-level proxy so ``from .config import settings`` always resolves
    to the current cached Settings instance (fresh after cache_clear())."""

    def __getattr__(self, name: str):
        return getattr(get_settings(), name)

    def __setattr__(self, name: str, value) -> None:
        setattr(get_settings(), name, value)

    def __dir__(self):
        return dir(get_settings())


settings = _SettingsProxy()
