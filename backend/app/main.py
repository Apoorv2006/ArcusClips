"""ArcusClips FastAPI application entry point."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .auth import ensure_default_admin
from .config import settings
from .db import get_session_factory, init_db
from .services.jobs import Worker, set_current_worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("arcusclips")

_worker: Worker | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _worker
    settings.ensure_dirs()
    init_db()
    session = get_session_factory()()
    try:
        ensure_default_admin(session)
        session.commit()
    finally:
        session.close()
    _worker = Worker()
    _worker.start()
    logger.info("%s started on %s:%s", settings.app_name, settings.host, settings.port)
    yield
    if _worker:
        _worker.stop()
        set_current_worker(None)


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # local-first app; tokens are required for API access
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from .api.routes_batches import router as batches_router
    from .api.routes_core import router as core_router
    from .api.routes_sources import router as sources_router

    app.include_router(core_router)
    app.include_router(sources_router)
    app.include_router(batches_router)

    @app.get("/api/health")
    def health():
        from .services import ffmpeg_engine, lmstudio

        return {
            "status": "ok",
            "app": settings.app_name,
            "ffmpeg": ffmpeg_engine.ffmpeg_version(),
            "lmstudio_connected": lmstudio.is_available(),
        }

    # serve the built frontend (frontend/dist) when present
    dist = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="frontend")
    return app


app = create_app()
