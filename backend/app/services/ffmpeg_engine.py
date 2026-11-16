"""FFmpeg / FFprobe engine: media probing, audio extraction, ASS caption
generation (6 styles), layout filters and clip rendering.

Uses the bundled static FFmpeg binary (imageio-ffmpeg) by default. If a real
``ffprobe`` binary is configured, it is used for probing; otherwise a faithful
JSON emulation built on ``ffmpeg -i`` is used.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import settings

logger = logging.getLogger("arcusclips.ffmpeg")

CAPTION_STYLES = ("bounce", "hype", "clean", "box", "karaoke", "ticker")
LAYOUTS = ("full", "blur", "zoom")


# ---------------------------------------------------------------------------
# Binary resolution
# ---------------------------------------------------------------------------
def get_ffmpeg_path() -> str:
    if settings.ffmpeg_path and Path(settings.ffmpeg_path).exists():
        return settings.ffmpeg_path
    if shutil.which("ffmpeg"):
        return shutil.which("ffmpeg")  # type: ignore[return-value]
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "FFmpeg binary not found. Install FFmpeg or set ARCUS_FFMPEG_PATH."
        ) from exc


def get_ffprobe_path() -> str | None:
    """Return a real ffprobe binary path, or None if not available.

    ``imageio-ffmpeg`` supplies only FFmpeg, not FFprobe — it is never reported
    as a FFprobe source. On Windows, install FFmpeg (which bundles ffprobe) and
    set ``ARCUS_FFPROBE_PATH`` if it is not on PATH.
    """
    if settings.ffprobe_path and Path(settings.ffprobe_path).exists():
        return settings.ffprobe_path
    return shutil.which("ffprobe")


def ffprobe_available() -> bool:
    return get_ffprobe_path() is not None


def ffprobe_status() -> dict[str, Any]:
    """Actionable FFprobe status for the health/status endpoint."""
    path = get_ffprobe_path()
    if path:
        return {"available": True, "path": path, "detail": "ffprobe found"}
    return {
        "available": False,
        "path": "",
        "detail": (
            "ffprobe not found. The bundled imageio-ffmpeg provides FFmpeg only. "
            "Install FFmpeg (bundles ffprobe) or set ARCUS_FFPROBE_PATH. "
            "Media probing will use the FFmpeg stderr fallback until then."
        ),
    }


def ffmpeg_version() -> str:
    try:
        out = subprocess.run(
            [get_ffmpeg_path(), "-version"], capture_output=True, text=True, timeout=30
        )
        return out.stdout.splitlines()[0] if out.stdout else "unknown"
    except Exception as exc:
        return f"error: {exc}"


def _run(cmd: list[str], timeout: float | None = None, cwd: str | None = None) -> subprocess.CompletedProcess:
    logger.debug("Running: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)


def _filter_path(path: str) -> str:
    """Escape an absolute path for embedding in a filtergraph option value.

    FFmpeg parses filter arguments with ':' as the option separator and '\\' as
    an escape character, so unquoted Windows paths ('C:\\Users\\...') break the
    ass/subtitles filters ('C' becomes the filename). Forward slashes avoid
    backslash escaping entirely; the drive-letter colon is escaped and the value
    is single-quoted so spaces, commas and emoji pass through.
    """
    s = str(path).replace("\\", "/")
    s = s.replace(":", r"\:").replace("'", r"\'")
    return f"'{s}'"



# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------
@dataclass
class MediaInfo:
    path: str
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_audio: bool = False
    has_video: bool = False
    video_codec: str = ""
    audio_codec: str = ""
    size: int = 0
    valid: bool = False
    detail: str = ""
    streams: list[dict[str, Any]] = field(default_factory=list)


def _parse_ffmpeg_info(stderr: str) -> dict[str, Any]:
    """Parse `ffmpeg -i` stderr output into a structured summary (ffprobe fallback)."""
    result: dict[str, Any] = {"streams": [], "duration": 0.0}
    dur = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    if dur:
        h, m, s = dur.groups()
        result["duration"] = int(h) * 3600 + int(m) * 60 + float(s)
    for raw_line in stderr.splitlines():
        line = raw_line.strip()
        if not line.startswith("Stream #"):
            continue
        m = re.match(
            r"Stream #(\d+):(\d+)\s*(\[[^\]]*\]\([^)]*\))?\s*:\s*(Video|Audio)\s*[,:]?\s*([^,]+)?",
            line,
        )
        if not m:
            continue
        idx, kind, _tag, codec_type, codec = m.groups()
        stream = {"index": int(idx), "codec_type": codec_type, "codec": (codec or "").strip()}
        dim = re.search(r"(\d{2,5})x(\d{2,5})", line)
        if dim:
            stream["width"], stream["height"] = int(dim.group(1)), int(dim.group(2))
        fps_m = re.search(r"(\d+(?:\.\d+)?)\s*fps", line)
        if fps_m:
            stream["fps"] = float(fps_m.group(1))
        result["streams"].append(stream)
    return result


def probe(path: str | Path) -> MediaInfo:
    """Probe a media file with ffprobe (or the ffmpeg-based fallback)."""
    path = str(path)
    if not Path(path).exists():
        return MediaInfo(path=path, detail="File does not exist")
    info = MediaInfo(path=path, size=Path(path).stat().st_size)
    probe_bin = get_ffprobe_path()
    if probe_bin:
        proc = _run(
            [
                probe_bin, "-v", "quiet", "-print_format", "json",
                "-show_format", "-show_streams", path,
            ],
            timeout=120,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                data = json.loads(proc.stdout)
                fmt = data.get("format", {})
                info.duration = float(fmt.get("duration", 0.0) or 0.0)
                for st in data.get("streams", []):
                    info.streams.append(st)
                    if st.get("codec_type") == "video":
                        info.has_video = True
                        info.width = int(st.get("width", 0) or 0)
                        info.height = int(st.get("height", 0) or 0)
                        info.video_codec = st.get("codec_name", "")
                        if st.get("avg_frame_rate"):
                            num, _, den = str(st["avg_frame_rate"]).partition("/")
                            if den and float(den) != 0:
                                info.fps = float(num) / float(den)
                    elif st.get("codec_type") == "audio":
                        info.has_audio = True
                        info.audio_codec = st.get("codec_name", "")
            except (json.JSONDecodeError, ValueError):
                logger.warning("ffprobe returned invalid JSON; falling back to ffmpeg -i")
        else:
            logger.warning("ffprobe failed for %s: %s", path, proc.stderr[:200])
    if not info.streams:
        if not ffprobe_available():
            logger.warning(
                "ffprobe not available for %s — using FFmpeg stderr fallback for "
                "media probing. Install FFmpeg/ffprobe or set ARCUS_FFPROBE_PATH.",
                path,
            )
        proc = _run([get_ffmpeg_path(), "-i", path], timeout=120)
        parsed = _parse_ffmpeg_info(proc.stderr)
        info.duration = parsed["duration"]
        info.streams = parsed["streams"]
        for st in info.streams:
            ctype = (st.get("codec_type") or "").lower()
            if ctype == "video":
                info.has_video = True
                info.width = int(st.get("width", 0) or 0)
                info.height = int(st.get("height", 0) or 0)
                info.video_codec = st.get("codec", "")
                info.fps = float(st.get("fps", 0) or 0)
            elif ctype == "audio":
                info.has_audio = True
                info.audio_codec = st.get("codec", "")
    info.valid = info.has_video and info.duration > 0
    info.detail = (
        f"{info.width}x{info.height} {info.duration:.1f}s "
        f"video={info.video_codec} audio={info.audio_codec or 'none'}"
    )
    return info


# ---------------------------------------------------------------------------
# Layout filters
# ---------------------------------------------------------------------------
def _layout_filter(layout: str, w: int, h: int) -> str:
    """Return the FFmpeg filter chain that turns source video into 1080x1920."""
    tw, th = settings.render_width, settings.render_height
    if layout == "blur":
        return (
            f"[0:v]split=2[bg][fg];"
            f"[bg]scale={tw}:{th}:force_original_aspect_ratio=increase,"
            f"crop={tw}:{th},boxblur=20:4[bg];"
            f"[fg]scale={tw}:-2:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2"
        )
    if layout == "zoom":
        # scale up to cover 9:16 with a slight zoom, then center-crop
        return (
            f"[0:v]scale={tw}:{th}:force_original_aspect_ratio=increase,"
            f"crop={tw}:{th}"
        )
    # full: center-crop native resolution to 9:16 (clamped to source bounds), then scale
    return (
        f"[0:v]crop=min(iw\\,ih*{tw}/{th}):min(ih\\,iw*{th}/{tw}),"
        f"scale={tw}:{th}"
    )


# ---------------------------------------------------------------------------
# ASS caption generation
# ---------------------------------------------------------------------------
def _ass_header(style_name: str, font_size: int, primary: str, outline: str,
                bold: bool, margin_v: int, alignment: int = 2) -> str:
    bold_tag = 1 if bold else 0
    fmt_line = (
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
        "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
    )
    def _style(name: str, size: int, prim: str, out: str, b: int, al: int, mv: int) -> str:
        return (
            f"Style: {name},DejaVu Sans,{size},{prim},{out},{out},{b},0,0,0,"
            f"100,100,0,0,1,Outline,1,{al},60,60,{mv},1"
        )
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {settings.render_width}\n"
        f"PlayResY: {settings.render_height}\n"
        "WrapStyle: 0\n"
        "\n"
        "[V4+ Styles]\n"
        f"{fmt_line}\n"
        f"{_style(style_name, font_size, primary, outline, bold_tag, alignment, margin_v)}\n"
        f"{_style('Hook', font_size + 26, '&H00FFFFFF', outline, 1, 8, 0)}\n"
        f"{_style('Handle', font_size // 2, primary, outline, 1, 8, 0)}\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )


def _ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _escape(text: str) -> str:
    return text.replace("\\", "\\N").replace("\n", "\\N")


def _words_from_segments(segments: list[dict[str, Any]], start: float, end: float) -> list[tuple[str, float, float]]:
    """Split transcript segments in [start,end] into per-word timed events.

    Uses word-level timestamps when present; otherwise distributes the segment
    duration evenly across its words.
    """
    words: list[tuple[str, float, float]] = []
    for seg in segments:
        s0, e0 = float(seg.get("start", 0.0)), float(seg.get("end", 0.0))
        if e0 <= start or s0 >= end:
            continue
        seg_words = seg.get("words") or []
        text = seg.get("text", "").strip()
        if not text:
            continue
        s0c, e0c = max(s0, start), min(e0, end)
        if seg_words and len(seg_words) == seg.get("word_count", len(seg_words)):
            for w in seg_words:
                ws, we = float(w.get("start", s0c)), float(w.get("end", e0c))
                if we <= start or ws >= end:
                    continue
                t = str(w.get("word", "")).strip()
                if t:
                    words.append((t, max(ws, start), min(we, end)))
        else:
            toks = re.findall(r"\S+", text)
            dur = max(e0c - s0c, 0.001)
            per = dur / max(len(toks), 1)
            for i, tok in enumerate(toks):
                words.append((tok, s0c + i * per, s0c + (i + 1) * per))
    return words


def generate_ass(
    out_path: str | Path,
    words: list[tuple[str, float, float]],
    style: str,
    handle: str = "",
    hook: str = "",
) -> str:
    """Generate an ASS subtitle file for the given style."""
    tw, th = settings.render_width, settings.render_height
    lines = []
    style_cfg = {
        "bounce":   dict(size=78, primary="&H00FFFFFF", outline="&H00000000", bold=True,  margin=260),
        "hype":     dict(size=84, primary="&H0000D4FF", outline="&H00000000", bold=True,  margin=240),
        "clean":    dict(size=64, primary="&H00FFFFFF", outline="&H96000000", bold=False, margin=300),
        "box":      dict(size=66, primary="&H00FFFFFF", outline="&H00000000", bold=False, margin=260),
        "karaoke":  dict(size=76, primary="&H00FFFFFF", outline="&H00000000", bold=True,  margin=280),
        "ticker":   dict(size=52, primary="&H00FFFFFF", outline="&H00000000", bold=True,  margin=60),
    }[style]
    lines.append(_ass_header(style, style_cfg["size"], style_cfg["primary"],
                             style_cfg["outline"], style_cfg["bold"], style_cfg["margin"],
                             alignment=8 if style == "ticker" else 2))

    if hook and style != "ticker":
        # hook overlay: large text fading in at the top, shown for the first ~3.5s
        lines.append(
            "Dialogue: 1,0:00:00.00,0:00:03.50,Hook,,0,0,0,,"
            f"{{\\fad(150,200)\\fscx105\\fscy105\\fsp2\\b1}}{_escape(hook[:60])}"
        )

    def word_event(idx: int, text: str, start: float, end: float) -> str:
        if style == "bounce":
            anim = "{\\fad(70,70)\\t(0,90,{\\fscx118\\fscy118})\\t(90,180,{\\fscx100\\fscy100})}"
        elif style == "hype":
            anim = "{\\fad(40,90)\\t(0,110,{\\fscx140\\fscy140})\\t(110,220,{\\fscx100\\fscy100})\\b1}"
        elif style == "clean":
            anim = "{\\fad(180,150)}"
        elif style == "box":
            anim = "{\\fad(120,120)}"
        elif style == "karaoke":
            anim = "{\\fad(50,60)\\t(0,80,{\\fscx115\\fscy115})\\t(80,160,{\\fscx100\\fscy100})}"
        else:  # ticker handled separately
            anim = "{\\fad(60,60)}"
        if style == "box":
            bw = max(len(text) * 38, 400)
            bh = style_cfg["size"] + 36
            x0 = (tw - bw) // 2
            y0 = th - style_cfg["margin"] - bh // 2 - style_cfg["size"]
            draw = (
                "{\\p1}m "
                f"{x0} {y0} l {x0 + bw} {y0} {x0 + bw} {y0 + bh} l {x0} {y0 + bh} {x0} {y0} "
                "{\\p0}\\1a&H8C&"
            )
            anim = f"{draw}{anim}"
        if style == "ticker":
            # each word scrolls right -> left across the top
            span = tw + 400
            t_dur = max(end - start, 0.4)
            anim = (
                f"{{\\fad(40,40)\\move({tw + 200} {style_cfg['margin']} "
                f"-220 {style_cfg['margin']},0,{t_dur * 1000:.0f})}}"
            )
        return f"Dialogue: 0,{_ts(start)},{_ts(end)},{style},,0,0,0,,{anim}{_escape(text)}"

    for i, (text, ws, we) in enumerate(words):
        lines.append(word_event(i, text, ws, we))

    if handle:
        y = 90 if style == "ticker" else 40
        lines.append(
            "Dialogue: 2,0:00:00.40,0:01:00.00,Handle,,0,0,0,,"
            f"{{\\fad(200,200)\\fs{style_cfg['size'] // 2}\\b1}}{_escape(handle)}"
        )
        # y adjusted by alignment of Handle style via inline \pos
        lines[-1] = lines[-1].replace("{\\fad(200,200)", f"{{\\fad(200,200)\\pos(0,{y})", 1)

    Path(out_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(out_path)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _fontsdir() -> str | None:
    """Directory containing fonts for libass (bundled DejaVu when present)."""
    candidate = settings.data_path / "fonts"
    if candidate.is_dir() and any(candidate.glob("*.ttf")):
        return str(candidate)
    return None


def extract_audio(video_path: str, out_wav: str, timeout: float = 1800) -> str:
    """Extract mono 16kHz PCM audio for Whisper."""
    cmd = [
        get_ffmpeg_path(), "-y", "-i", video_path,
        "-vn", "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le", out_wav,
    ]
    proc = _run(cmd, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"Audio extraction failed: {proc.stderr[-400:]}")
    return out_wav


def render_clip(
    source_path: str,
    start: float,
    end: float,
    out_path: str,
    style: str = "bounce",
    layout: str = "blur",
    handle: str = "",
    hook: str = "",
    words: list[tuple[str, float, float]] | None = None,
    media: MediaInfo | None = None,
    timeout: float = 1800,
) -> dict[str, Any]:
    """Render a vertical 9:16 clip with burned-in captions.

    Returns {render_path, duration, width, height, size, ok, error}.
    """
    if style not in CAPTION_STYLES:
        style = "bounce"
    if layout not in LAYOUTS:
        layout = "blur"
    start = max(0.0, start)
    duration = max(0.1, end - start)

    if media is None:
        media = probe(source_path)

    workdir = Path(out_path).parent
    workdir.mkdir(parents=True, exist_ok=True)
    # Unique caption sidecar name with no path separators/colons/spaces: ffmpeg
    # runs with cwd=workdir and references it by basename, so the filtergraph
    # never carries an absolute path (see _filter_path for why that matters).
    ass_name = f".captions_{uuid.uuid4().hex}.ass"
    ass_path = workdir / ass_name
    if words:
        generate_ass(str(ass_path), words, style, handle=handle, hook=hook)

    # Single filter_complex chain: layout -> burned-in ASS captions, output [v]
    fc = _layout_filter(layout, media.width, media.height)
    fonts = _fontsdir()
    if words:
        ass_arg = f"ass={ass_name}" + (f":fontsdir={_filter_path(fonts)}" if fonts else "")
        if layout == "blur":
            fc = fc.replace(
                "[bg][fg]overlay=(W-w)/2:(H-h)/2",
                "[bg][fg]overlay=(W-w)/2:(H-h)/2," + ass_arg,
            )
        else:
            fc = f"{fc},{ass_arg}"
    fc = f"{fc},format=yuv420p[v]"

    cmd = [
        get_ffmpeg_path(), "-y",
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
        "-i", source_path,
        "-filter_complex", fc,
        "-map", "[v]",
    ]
    if media.has_audio:
        cmd += ["-map", "0:a?"]
    cmd += [
        "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
        "-c:v", "libx264", "-preset", settings.render_preset, "-crf", str(settings.render_crf),
        "-pix_fmt", "yuv420p", "-r", str(settings.render_fps),
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
        "-movflags", "+faststart",
        "-t", f"{duration:.3f}",
        out_path,
    ]

    proc = _run(cmd, timeout=timeout, cwd=str(workdir))
    if proc.returncode != 0:
        err = proc.stderr[-800:]
        if Path(out_path).exists():
            Path(out_path).unlink(missing_ok=True)
        return {"ok": False, "error": f"FFmpeg render failed: {err}", "render_path": ""}

    rendered = Path(out_path)
    info = probe(out_path)
    if not info.valid:
        return {"ok": False, "error": "Rendered file failed validation", "render_path": ""}
    try:
        Path(ass_path).unlink(missing_ok=True)
    except OSError:
        pass
    return {
        "ok": True,
        "render_path": str(out_path),
        "duration": info.duration,
        "width": info.width,
        "height": info.height,
        "size": rendered.stat().st_size,
        "error": "",
    }


def make_thumbnail(source_path: str, at: float, out_path: str, timeout: float = 120) -> str:
    cmd = [
        get_ffmpeg_path(), "-y", "-ss", f"{max(0.0, at):.2f}", "-i", source_path,
        "-frames:v", "1", "-q:v", "3", out_path,
    ]
    proc = _run(cmd, timeout=timeout)
    if proc.returncode != 0 or not Path(out_path).exists():
        raise RuntimeError(f"Thumbnail failed: {proc.stderr[-200:]}")
    return out_path


def normalize_audio(in_path: str, out_path: str, timeout: float = 1800) -> str:
    cmd = [
        get_ffmpeg_path(), "-y", "-i", in_path,
        "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
        "-c:a", "aac", "-b:a", "192k", out_path,
    ]
    proc = _run(cmd, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"Audio normalization failed: {proc.stderr[-300:]}")
    return out_path
