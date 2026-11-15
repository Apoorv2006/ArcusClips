"""Moment selection: semantic analysis via LM Studio, validation of LLM
output, deterministic local heuristics fallback, and candidate ranking /
deduplication.

The LLM never transcribes audio (Whisper does that). The LLM returns
structured candidate moments which are validated against the actual source
duration and transcript segments; invalid or hallucinated timestamps are
rejected.
"""
from __future__ import annotations

import logging
import math
import re
from typing import Any

from ..config import settings
from . import lmstudio

logger = logging.getLogger("arcusclips.selection")

# heuristic signals
_QUESTION = re.compile(r"\b(why|how|what|when|who|where|which|can you|do you|is it|are you)\b", re.I)
_INTENSITY = re.compile(
    r"\b(amazing|incredible|insane|crazy|epic|huge|massive|exactly|critical|essential|"
    r"important|secret|surprising|shocking|confident|proven|guaranteed|never|always)\b",
    re.I,
)
_OPINION = re.compile(r"\b(i think|in my opinion|believe|honestly|frankly|the truth)\b", re.I)


class SelectionError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Segment utilities
# ---------------------------------------------------------------------------
def segments_text(segments: list[dict[str, Any]]) -> str:
    return " ".join(s.get("text", "") for s in segments).strip()


def candidate_window(segments: list[dict[str, Any]], center_idx: int, radius: int = 4) -> dict[str, Any]:
    """Return a context window of segments around a center segment."""
    start = max(0, center_idx - radius)
    end = min(len(segments), center_idx + radius + 1)
    win = segments[start:end]
    return {
        "start": win[0]["start"],
        "end": win[-1]["end"],
        "text": segments_text(win),
    }


def build_prompt(
    video_title: str,
    duration: float,
    transcript_text: str,
    segment_summaries: list[dict[str, Any]],
    clip_count: int,
    min_sec: float,
    max_sec: float,
    quality: str,
) -> str:
    threshold = {
        "strict": "only moments scoring >= 8/10 that stand fully on their own",
        "balanced": "moments scoring >= 6/10 that stand reasonably on their own",
        "max_coverage": "moments scoring >= 4/10, maximize coverage of distinct topics",
    }.get(quality, "moments scoring >= 6/10")

    # limit transcript text to the model's context budget
    max_chars = max(2000, settings.lmstudio_max_context_tokens * 3)
    transcript_text = transcript_text[:max_chars]

    return (
        "You are a short-form video editor. Given the transcript of a video, "
        "identify the best moments for standalone vertical short-form clips.\n\n"
        f"Video title: {video_title}\n"
        f"Duration: {duration:.0f} seconds\n"
        f"Required number of clips: {clip_count}\n"
        f"Clip duration range: {min_sec:.0f}-{max_sec:.0f} seconds\n"
        f"Selection policy: {threshold}.\n\n"
        "Transcript with timestamps (segments):\n"
        f"{transcript_text}\n\n"
        "Segment reference list (id: start-end):\n"
        + "\n".join(
            f"{i}: {s['start']:.1f}-{s['end']:.1f} {s.get('text', '')[:120]}"
            for i, s in enumerate(segment_summaries[:300])
        )
        + "\n\n"
        "Return ONLY valid JSON with this schema:\n"
        "{\n"
        '  "candidates": [\n'
        "    {\n"
        '      "start": <seconds, float, must fall within a transcript segment boundary>,\n'
        '      "end": <seconds, float, must fall within a transcript segment boundary>,\n'
        '      "transcript_excerpt": "<exact words from the transcript>",\n'
        '      "score": <0-10 number>,\n'
        '      "reason": "<why this moment works as a standalone clip>",\n'
        '      "hook": "<suggested opening hook, <= 60 chars>",\n'
        '      "title": "<suggested clip title>",\n'
        '      "description": "<suggested description>",\n'
        '      "category": "<one short category label or empty>"\n'
        "    }\n"
        "  ]\n"
        "}\n"
        f"Return exactly {clip_count} candidates if possible, fewer only if the "
        "transcript genuinely has fewer strong moments. Never invent timestamps "
        "outside the transcript range."
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_candidates(
    candidates: list[dict[str, Any]],
    duration: float,
    segments: list[dict[str, Any]],
    min_sec: float,
    max_sec: float,
) -> list[dict[str, Any]]:
    """Reject invalid/hallucinated candidates. Returns the valid subset."""
    valid: list[dict[str, Any]] = []
    seg_bounds = [(float(s.get("start", 0)), float(s.get("end", 0))) for s in segments]
    for c in candidates:
        try:
            start = float(c["start"])
            end = float(c["end"])
            score = float(c.get("score", 0))
        except (TypeError, ValueError):
            logger.warning("Rejected candidate with non-numeric timestamps: %s", c)
            continue
        if not (0 <= start < end <= duration + 0.5):
            logger.warning("Rejected out-of-range candidate %.1f-%.1f (duration %.1f)", start, end, duration)
            continue
        if not (min_sec <= (end - start) <= max_sec + 5):
            logger.warning("Rejected candidate with bad duration %.1f-%.1f", start, end)
            continue
        if not c.get("transcript_excerpt", "").strip():
            continue
        # excerpt must actually appear in the transcript (guards hallucination)
        excerpt_norm = re.sub(r"\s+", " ", c["transcript_excerpt"]).strip().lower()
        transcript_norm = re.sub(r"\s+", " ", segments_text(segments)).strip().lower()
        if excerpt_norm and excerpt_norm not in transcript_norm:
            # tolerate tiny mismatches: check first 25 chars
            if not excerpt_norm[:25] or excerpt_norm[:25] not in transcript_norm:
                logger.warning("Rejected candidate with excerpt not found in transcript")
                continue
        valid.append(
            {
                "start": round(start, 2),
                "end": round(end, 2),
                "transcript_excerpt": c.get("transcript_excerpt", "").strip(),
                "score": max(0.0, min(10.0, score)),
                "reason": str(c.get("reason", "")).strip(),
                "hook": str(c.get("hook", "")).strip()[:80],
                "title": str(c.get("title", "")).strip()[:200],
                "description": str(c.get("description", "")).strip()[:500],
                "category": str(c.get("category", "")).strip()[:60],
            }
        )
    return valid


def snap_to_segments(candidates: list[dict[str, Any]], segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Round candidate boundaries to nearest transcript segment boundaries
    so clips never cut mid-sentence."""
    bounds = sorted({float(s.get("start", 0)) for s in segments} | {float(s.get("end", 0)) for s in segments})
    for c in candidates:
        for key in ("start", "end"):
            val = c[key]
            nearest = min(bounds, key=lambda b: abs(b - val))
            c[key] = round(nearest, 2)
        if c["end"] <= c["start"]:
            c["end"] = round(c["start"] + 5, 2)
    return candidates


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------
def _overlap(a: dict[str, Any], b: dict[str, Any]) -> float:
    lo = max(a["start"], b["start"])
    hi = min(a["end"], b["end"])
    if hi <= lo:
        return 0.0
    denom = min(a["end"] - a["start"], b["end"] - b["start"])
    if denom <= 0:
        return 0.0
    return (hi - lo) / denom


def _similar(a: dict[str, Any], b: dict[str, Any]) -> bool:
    import re as _re

    ta = set(_re.findall(r"\w+", a["transcript_excerpt"].lower()))
    tb = set(_re.findall(r"\w+", b["transcript_excerpt"].lower()))
    if not ta or not tb:
        return False
    return len(ta & tb) / min(len(ta), len(tb)) > 0.7


def dedupe_candidates(candidates: list[dict[str, Any]], overlap_threshold: float = 0.35) -> list[dict[str, Any]]:
    """Remove candidates that substantially overlap or repeat each other."""
    ranked = sorted(candidates, key=lambda c: -c["score"])
    kept: list[dict[str, Any]] = []
    for cand in ranked:
        ok = True
        for k in kept:
            if _overlap(cand, k) > overlap_threshold or _similar(cand, k):
                ok = False
                break
        if ok:
            kept.append(cand)
    return kept


# ---------------------------------------------------------------------------
# Heuristics (fallback when LM Studio is unavailable)
# ---------------------------------------------------------------------------
def heuristic_candidates(
    transcript_text: str,
    segments: list[dict[str, Any]],
    duration: float,
    clip_count: int,
    min_sec: float,
    max_sec: float,
) -> list[dict[str, Any]]:
    """Deterministic local selection heuristics — clearly flagged as heuristic."""
    scored: list[tuple[float, dict[str, Any]]] = []
    seg_len = max(1, len(segments))
    for i, seg in enumerate(segments):
        start = float(seg.get("start", 0))
        end = float(seg.get("end", 0))
        text = seg.get("text", "")
        if not text.strip():
            continue
        score = 5.0
        if _QUESTION.search(text):
            score += 1.5
        if _INTENSITY.search(text):
            score += 1.2
        if _OPINION.search(text):
            score += 0.8
        if re.search(r"\d", text):
            score += 0.4
        # reward segments away from the very start/end (less intro/outro filler)
        score += 0.5 * (1 - min(1.0, (min(start, duration - end) / max(duration / 4, 1))))
        # prefer central coverage
        score += 0.3 * (1 - abs((start + end) / 2 / duration - 0.5) * 2)
        scored.append((score, {"segment_index": i, "start": start, "end": end, "text": text}))

    # build windows around top segments
    ranked = sorted(scored, key=lambda t: -t[0])
    chosen: list[dict[str, Any]] = []
    for score, seg in ranked:
        if len(chosen) >= clip_count:
            break
        if any(_overlap({"start": seg["start"], "end": seg["end"]}, c) > 0.3 for c in chosen):
            continue
        window_start = max(0.0, seg["start"] - (max_sec - (seg["end"] - seg["start"])) / 2)
        window_end = min(duration, window_start + max_sec)
        window_start = max(0.0, window_end - max_sec)
        if window_end - window_start < min_sec:
            window_end = min(duration, window_start + min_sec)
        ctx = candidate_window(segments, seg["segment_index"])
        hook = re.sub(r"^[^a-zA-Z]*", "", ctx["text"])[:60]
        chosen.append(
            {
                "start": round(window_start, 2),
                "end": round(window_end, 2),
                "transcript_excerpt": ctx["text"][:200],
                "score": round(min(10.0, score), 2),
                "reason": "Selected by deterministic local heuristics (LM Studio unavailable).",
                "hook": hook or "Watch this",
                "title": re.sub(r"\W+", " ", ctx["text"][:60]).strip() or "Clip",
                "description": ctx["text"][:280],
                "category": "",
            }
        )
    return chosen


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def select_moments(
    video_title: str,
    duration: float,
    segments: list[dict[str, Any]],
    clip_count: int,
    min_sec: float,
    max_sec: float,
    quality: str = "balanced",
) -> dict[str, Any]:
    """Rank and select the best moments. Uses LM Studio when available,
    falls back to local heuristics (flagged) when it is not."""
    transcript_text = segments_text(segments)
    if not transcript_text.strip():
        return {"candidates": [], "source": "empty", "error": "Transcript is empty"}

    use_llm = False
    error = ""
    candidates: list[dict[str, Any]] = []
    if lmstudio.is_available():
        try:
            prompt = build_prompt(video_title, duration, transcript_text, segments,
                                  clip_count, min_sec, max_sec, quality)
            data = lmstudio.chat_json([{"role": "user", "content": prompt}])
            raw = data.get("candidates") if isinstance(data, dict) else None
            if not isinstance(raw, list):
                raise SelectionError("LLM did not return a candidates array")
            candidates = validate_candidates(raw, duration, segments, min_sec, max_sec)
            candidates = snap_to_segments(candidates, segments)
            candidates = dedupe_candidates(candidates)
            use_llm = True
        except Exception as exc:
            error = str(exc)
            logger.warning("LM Studio selection failed: %s", error)
    else:
        error = "LM Studio is not reachable"

    if not candidates and settings.allow_heuristic_fallback:
        logger.info("Using deterministic heuristic fallback for moment selection")
        candidates = heuristic_candidates(transcript_text, segments, duration,
                                          clip_count, min_sec, max_sec)
        candidates = snap_to_segments(candidates, segments)
        source = "heuristic"
    else:
        source = "lmstudio" if use_llm else "none"

    # final cap
    if clip_count > 0:
        candidates = sorted(candidates, key=lambda c: -c["score"])[:clip_count]

    return {"candidates": candidates, "source": source, "error": error if not use_llm else ""}
