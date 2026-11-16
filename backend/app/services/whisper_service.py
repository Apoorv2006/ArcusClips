"""Local Whisper transcription using faster-whisper.

Real speech-to-text with word-level timestamps, model caching, language
detection and progress reporting. Runs on CPU by default; CUDA optional.
"""
from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Callable

from ..config import settings

logger = logging.getLogger("arcusclips.whisper")

ProgressCb = Callable[[float, str], None]

_model_cache: dict[str, Any] = {}


def _no_progress(_p: float, _msg: str) -> None:
    pass


def _get_model():
    """Load (and cache) the faster-whisper model."""
    key = f"{settings.whisper_model}:{settings.whisper_device}:{settings.whisper_compute_type}"
    if key in _model_cache:
        return _model_cache[key]
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "faster-whisper is not installed. Run: pip install -r backend/requirements.txt"
        ) from exc
    kwargs: dict[str, Any] = {"device": settings.whisper_device, "compute_type": settings.whisper_compute_type}
    if settings.whisper_model_dir:
        kwargs["download_root"] = settings.whisper_model_dir
    try:
        model = WhisperModel(settings.whisper_model, **kwargs)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to load Whisper model '{settings.whisper_model}'. "
            "The first run downloads model weights from huggingface.co; ensure network "
            "access or set ARCUS_WHISPER_MODEL_DIR to a local model directory. "
            f"Original error: {exc}"
        ) from exc
    _model_cache[key] = model
    return model


def file_hash(path: str | Path) -> str:
    """Streamed MD5 hash (used to skip transcription of unchanged sources)."""
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _decode_audio_f32(audio_path: str, sampling_rate: int = 16000):
    """Decode an audio file to float32 mono samples using FFmpeg.

    faster-whisper passes ``metadata_errors`` to ``av.open()``, a parameter
    that only exists in PyAV >= 11. If the environment has an older PyAV,
    faster-whisper's own ``decode_audio`` raises ``TypeError`` before touching
    the model. The media is already on disk, so decoding with FFmpeg (bundled
    via imageio-ffmpeg) produces the exact same f32le PCM that decode_audio
    yields — float32 in [-1, 1], 16 kHz mono — and the model accepts a samples
    array in place of a path.
    """
    import numpy as np

    from . import ffmpeg_engine

    cmd = [
        ffmpeg_engine.get_ffmpeg_path(),
        "-nostdin", "-loglevel", "error",
        "-i", str(audio_path),
        "-ac", "1", "-ar", str(sampling_rate),
        "-f", "f32le", "-acodec", "pcm_f32le",
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0 or not proc.stdout:
        detail = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ffmpeg failed to decode audio for transcription: {detail}")
    return np.frombuffer(proc.stdout, dtype=np.float32)


def transcribe_audio(
    audio_path: str,
    progress_cb: ProgressCb | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    """Transcribe a 16kHz PCM WAV file. Returns {language, duration, segments}.

    Segments are stored as [{start, end, text, words: [{word,start,end}], word_count}].
    """
    cb = progress_cb or _no_progress
    model = _get_model()
    cb(0.05, f"Loading Whisper model '{settings.whisper_model}'")
    transcribe_kwargs: dict[str, Any] = {
        "language": language or (settings.whisper_language or None),
        "word_timestamps": True,
        "beam_size": 5,
        "vad_filter": True,
    }
    try:
        segments_iter, info = model.transcribe(audio_path, **transcribe_kwargs)
    except TypeError as exc:
        if "metadata_errors" not in str(exc):
            raise
        # Environment has PyAV < 11 (or no PyAV): faster-whisper's decode_audio
        # is unusable. Decode with FFmpeg and feed the samples directly.
        logger.warning(
            "faster-whisper's audio decode failed (%s). Falling back to FFmpeg decode. "
            "Upgrade with: pip install -U av", exc,
        )
        samples = _decode_audio_f32(audio_path)
        segments_iter, info = model.transcribe(samples, **transcribe_kwargs)
    cb(0.15, f"Detected language: {info.language} (probability {info.language_probability:.2f})")
    segments: list[dict[str, Any]] = []
    total = info.duration or 1.0
    for seg in segments_iter:
        words = [
            {"word": w.word, "start": w.start, "end": w.end}
            for w in (seg.words or [])
        ]
        segments.append(
            {
                "start": seg.start,
                "end": seg.end,
                "text": seg.text.strip(),
                "words": words,
                "word_count": len(words),
            }
        )
        pct = min(0.95, (seg.end / total) if total else 0.9)
        cb(pct, f"Transcribed {seg.end:.0f}s / {total:.0f}s")
    cb(1.0, "Transcription complete")
    return {
        "language": info.language,
        "duration": total,
        "segments": segments,
        "model": settings.whisper_model,
    }


def transcribe_video_file(
    video_path: str,
    wav_path: str,
    progress_cb: ProgressCb | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    """Extract audio with FFmpeg then transcribe. Requires the media engine."""
    from . import ffmpeg_engine

    cb = progress_cb or _no_progress
    cb(0.02, "Extracting audio")
    ffmpeg_engine.extract_audio(video_path, wav_path)
    result = transcribe_audio(wav_path, progress_cb=progress_cb, language=language)
    return result


def persist_transcript(video_id: int, user_id: int, result: dict[str, Any],
                       source_hash: str, session) -> None:
    """Store a transcript record, replacing any previous one."""
    from ..models import Transcript

    session.query(Transcript).filter(Transcript.video_id == video_id).delete()
    session.add(
        Transcript(
            video_id=video_id,
            user_id=user_id,
            language=result["language"],
            model_name=result.get("model", settings.whisper_model),
            duration=result["duration"],
            segments=result["segments"],
            source_hash=source_hash,
        )
    )
    session.flush()


def transcript_to_dict(t) -> dict[str, Any]:
    return {
        "video_id": t.video_id,
        "language": t.language,
        "model_name": t.model_name,
        "duration": t.duration,
        "segments": t.segments or [],
        "created_at": t.created_at,
    }


def segments_as_text(segments: list[dict[str, Any]]) -> str:
    return " ".join(s.get("text", "") for s in segments).strip()


def segments_summary_json(segments: list[dict[str, Any]]) -> str:
    return json.dumps(segments, ensure_ascii=False)
