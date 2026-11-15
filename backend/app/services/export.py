"""Batch ZIP packaging with manifests.

Streams files into the archive (no whole-video RAM loading), preserves the
original Drive/page folder context, and verifies final archive integrity.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from ..config import settings
from ..db import utcnow
from ..models import Batch, Clip, ClipVersion, Export
from ..security import safe_filename

logger = logging.getLogger("arcusclips.export")


def _safe_join(root: Path, rel: str) -> Path:
    rel = rel.replace("\\", "/").lstrip("/")
    parts = [p for p in rel.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise ValueError("Unsafe path in archive")
    return root.joinpath(*parts)


def _manifest_rows(clips: list[Clip]) -> list[dict[str, Any]]:
    rows = []
    for clip in clips:
        ver = clip.versions[-1] if clip.versions else None
        rows.append(
            {
                "generated_filename": ver.render_path.rsplit("/", 1)[-1] if ver and ver.render_path else "",
                "source_name": clip.video.name if clip.video else "",
                "source_url": clip.video.origin_url if clip.video else "",
                "source_drive_id": (clip.video.drive_file.drive_file_id if clip.video and clip.video.drive_file else ""),
                "folder_path": clip.video.folder_path if clip.video else "",
                "start_seconds": clip.start_time,
                "end_seconds": clip.end_time,
                "duration_seconds": round(clip.end_time - clip.start_time, 2),
                "hook": clip.hook,
                "title": clip.title,
                "description": clip.description,
                "selection_score": clip.score,
                "caption_style": clip.caption_style,
                "layout": clip.layout,
                "processing_status": "ok" if ver and ver.status == "rendered" else "failed",
                "batch_id": clip.batch_id,
                "is_heuristic": clip.is_heuristic,
            }
        )
    return rows


def build_batch_zip(session: Session, user_id: int, batch_id: int) -> Export:
    """Build (or rebuild) the ZIP for a batch. Returns the Export record."""
    batch = session.get(Batch, batch_id)
    if not batch or batch.user_id != user_id:
        raise ValueError("Batch not found")

    existing = (
        session.query(Export)
        .filter(Export.batch_id == batch_id)
        .order_by(Export.created_at.desc())
        .first()
    )
    export = existing or Export(user_id=user_id, batch_id=batch_id, status="pending")
    if existing is None:
        session.add(export)
    export.status = "building"
    export.error = ""
    session.flush()

    try:
        clips = (
            session.query(Clip)
            .filter(Clip.batch_id == batch_id)
            .order_by(Clip.video_id, Clip.index)
            .all()
        )
        rendered = [
            c for c in clips
            if c.versions and c.versions[-1].status == "rendered" and c.versions[-1].render_path
        ]
        if not rendered:
            raise ValueError("Batch has no rendered clips to export")

        zip_path = settings.export_path / f"batch_{batch_id:03d}.zip"
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = zip_path.with_suffix(".zip.part")

        rows = _manifest_rows(rendered)
        summary = {
            "batch_id": batch.id,
            "batch_name": batch.name,
            "created_at": batch.created_at.isoformat() if batch.created_at else "",
            "finished_at": batch.finished_at.isoformat() if batch.finished_at else "",
            "status": batch.status,
            "options": batch.options,
            "clip_count": len(rows),
        }

        csv_buf = io.StringIO()
        fieldnames = list(rows[0].keys())
        writer = csv.DictWriter(csv_buf, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for row in rows:
                src = row["generated_filename"]
                # locate the actual file by basename within render dir
                cand = next(
                    (p for p in settings.render_path.rglob(src) if p.is_file()), None
                )
                if not cand:
                    continue
                rel_dir = row["folder_path"] or (row["source_name"] or "Misc")
                arc_video = f"clips/{safe_filename(rel_dir)}/{src}"
                zf.write(cand, arc_video)
            # thumbnails where available
            for clip in rendered:
                ver = clip.versions[-1]
                if ver.thumbnail_path and Path(ver.thumbnail_path).exists():
                    rel_dir = clip.video.folder_path if clip.video and clip.video.folder_path else (clip.video.name if clip.video else "Misc")
                    arc = f"thumbnails/{safe_filename(rel_dir)}/clip_{clip.id}.jpg"
                    zf.write(ver.thumbnail_path, arc)
            zf.writestr("metadata/manifest.csv", csv_buf.getvalue())
            zf.writestr("metadata/manifest.json", json.dumps(rows, ensure_ascii=False, indent=2))
            zf.writestr("metadata/batch_summary.json", json.dumps(summary, ensure_ascii=False, indent=2))

        # verify archive integrity
        with zipfile.ZipFile(tmp_path) as zf:
            bad = zf.testzip()
            if bad is not None:
                raise ValueError(f"Archive integrity check failed at {bad}")
            count = len(zf.namelist())

        tmp_path.replace(zip_path)
        export.zip_path = str(zip_path)
        export.status = "done"
        export.size = zip_path.stat().st_size
        export.clip_count = len(rows)
        session.flush()
        logger.info("Built batch ZIP %s (%d clips, %d bytes)", zip_path, len(rows), export.size)
    except Exception as exc:
        export.status = "failed"
        export.error = str(exc)[:2000]
        session.flush()
        raise
    return export


def export_row_bytes(clip: Clip, version: ClipVersion) -> bytes | None:
    """Read a single rendered clip into memory (small files only; used for
    individual downloads via FileResponse instead — see API)."""
    p = Path(version.render_path)
    if not p.exists():
        return None
    return p.read_bytes()
