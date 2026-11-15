"""Shared pytest fixtures: isolated data dir, API client, sample media."""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("ARCUS_AUTH_ENABLED", "true")


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory) -> str:
    """Generate a real 60s test source with video + audio via FFmpeg."""
    from app.services import ffmpeg_engine

    ff = ffmpeg_engine.get_ffmpeg_path()
    path = str(tmp_path_factory.mktemp("media") / "sample_source.mp4")
    subprocess.run(
        [ff, "-y", "-f", "lavfi", "-i", "testsrc=duration=60:size=640x360:rate=30",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=60",
         "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", path],
        capture_output=True, check=True,
    )
    return path


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    """Isolated settings + fresh database for each test."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "fonts").mkdir(exist_ok=True)
    monkeypatch.setenv("ARCUS_DATA_DIR", str(data_dir))
    # force a fresh Settings instance for this test
    from app.config import get_settings

    get_settings.cache_clear()
    # reset the module-level engine so the new data dir takes effect
    from app import db as dbmod

    dbmod._engine = None
    dbmod._SessionLocal = None
    settings = get_settings()
    settings.ensure_dirs()
    # copy bundled fonts if present in the repo data dir
    repo_fonts = Path(__file__).resolve().parents[2] / "data" / "fonts"
    if repo_fonts.is_dir():
        for f in repo_fonts.glob("*.ttf"):
            (data_dir / "fonts" / f.name).write_bytes(f.read_bytes())
    return settings


@pytest.fixture()
def client(app_env):
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def auth_headers(client) -> dict[str, str]:
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "changeme"})
    assert resp.status_code == 200, resp.text
    token = resp.json()["token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def uploaded_video(client, auth_headers, sample_video) -> dict:
    import shutil

    dest = Path(sample_video).with_name("upload_source.mp4")
    shutil.copy(sample_video, dest)
    with open(dest, "rb") as fh:
        resp = client.post(
            "/api/videos/upload",
            files={"file": ("upload_source.mp4", fh, "video/mp4")},
            headers=auth_headers,
        )
    assert resp.status_code == 200, resp.text
    return resp.json()
