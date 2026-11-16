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
# Speech-aware clip boundaries
# ---------------------------------------------------------------------------
# Hard duration cutoffs chop clips mid-word and mid-thought. The resolver
# below keeps the configured duration preset as a *target*, not a hard limit,
# and moves clip boundaries to the nearest natural speech boundary within a
# configurable tolerance (settings.clip_duration_tolerance_seconds).
#
# Deterministic rules, in priority order:
#   1. Never split a word — boundaries always land on word edges.
#   2. Prefer ending on sentence-ending punctuation (. ! ? …) — a complete thought.
#   3. Prefer natural pauses (silence between words) over mid-flow cuts.
#   4. Prefer segment breaks (topic/speaker changes) as weak completeness signals.
#   5. Among comparably complete boundaries, prefer the one nearest the target.
#   6. Beyond the tolerance budget, stay inside it; if even that is impossible,
#      end at the closest word edge to the target.
# When the transcript has no usable timestamps at all, the resolver uses
# FFmpeg silence detection to avoid cutting during active speech, and only
# falls back to plain duration-based clipping when silence is unavailable too.
_SENTENCE_END_RE = re.compile(r"[.!?…]+[\"'\)\]]*$")
_PAUSE_THRESHOLD = 0.55   # seconds of quiet that read as a deliberate pause
_STRONG_PAUSE = 1.0       # seconds of quiet that clearly mark a thought change
_MAX_TOLERANCE = 30.0     # safety clamp for the configurable tolerance


def _flatten_words(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten segment word-level timestamps into one chronological list."""
    words: list[dict[str, Any]] = []
    for seg_index, seg in enumerate(segments or []):
        for w in seg.get("words") or []:
            try:
                text = str(w.get("word", "")).strip()
                ws, we = float(w.get("start", 0.0)), float(w.get("end", 0.0))
            except (TypeError, ValueError):
                continue
            if not text or we <= ws:
                continue
            words.append({"text": text, "start": ws, "end": we, "segment": seg_index})
    words.sort(key=lambda w: (w["start"], w["end"]))
    return words


def _is_sentence_end(text: str) -> bool:
    return bool(_SENTENCE_END_RE.search(text))


def _boundary_score(words: list[dict[str, Any]], i: int, target_end: float) -> float:
    """Score word i as a possible clip end. Higher is better.

    Two-tier scoring so completeness always beats proximity: the tier
    (sentence end > pause/segment-break/end-of-speech > plain word edge)
    dominates; within a tier, nearer to the target wins and a pause bonus
    fine-tunes the choice."""
    w = words[i]
    t = w["end"]
    nxt = words[i + 1] if i + 1 < len(words) else None
    if nxt is None:
        gap = 5.0  # transcript speech ends here — a natural close
    else:
        gap = max(0.0, nxt["start"] - t)
    segment_break = nxt is not None and nxt["segment"] != w["segment"]
    pause_bonus = 0.0
    if gap >= _STRONG_PAUSE:
        pause_bonus = 20.0 + min(gap, 3.0) * 3.0
    elif gap >= _PAUSE_THRESHOLD:
        pause_bonus = 12.0 + gap * 6.0
    elif gap >= 0.30:
        pause_bonus = 3.0
    if _is_sentence_end(w["text"]):
        tier = 2
    elif pause_bonus > 0.0 or segment_break or nxt is None:
        tier = 1
    else:
        tier = 0
    if segment_break:
        pause_bonus += 3.0
    if nxt is None:
        pause_bonus += 5.0
    return tier * 1000.0 + pause_bonus - abs(t - target_end) * 5.0


def _clamp_tolerance(tolerance: float | None) -> float:
    try:
        tol = float(tolerance) if tolerance is not None else settings.clip_duration_tolerance_seconds
    except (TypeError, ValueError):
        tol = settings.clip_duration_tolerance_seconds
    return min(max(tol, 0.0), _MAX_TOLERANCE)


def _snap_start(words: list[dict[str, Any]], proposed: float, tolerance: float) -> float:
    """Snap a proposed start so it never splits a word and prefers the
    beginning of the sentence being entered (bounded by tolerance/2 so we do
    not swallow unrelated preceding dialogue)."""
    first = next((i for i, w in enumerate(words) if w["end"] > proposed), None)
    if first is None:
        return proposed
    # never split a word: if proposed lands inside one, move to its start
    start = min(proposed, words[first]["start"]) if words[first]["start"] < proposed else proposed
    # walk back to the start of the sentence containing `first`, bounded
    sentence_first = first
    while sentence_first > 0:
        prev = words[sentence_first - 1]
        cur = words[sentence_first]
        if _is_sentence_end(prev["text"]) or cur["start"] - prev["end"] >= _PAUSE_THRESHOLD:
            break
        sentence_first -= 1
    sentence_start = words[sentence_first]["start"]
    if proposed - sentence_start <= tolerance / 2.0:
        start = min(start, sentence_start)
    return start


def _choose_end(words: list[dict[str, Any]], start: float, target_end: float,
                duration: float, min_sec: float, tolerance: float) -> float:
    """Pick the clip end: a natural speech boundary within tolerance of the
    target when one exists, otherwise the word edge closest to the target."""
    earliest = start + max(1.0, min_sec - tolerance)
    latest = min(duration, target_end + tolerance)
    in_window = [i for i, w in enumerate(words) if earliest - 1e-6 <= w["end"] <= latest + 1e-6]
    if in_window:
        best = max(in_window, key=lambda i: _boundary_score(words, i, target_end))
        return words[best]["end"]
    # Nothing inside the allowed window. Trust the transcript tail only when
    # it lies within the tolerance budget of the target (speech really is
    # over); a thin word set (legacy transcripts, VAD gaps) must not collapse
    # the clip onto some distant last word — fall back to duration-based.
    below = [i for i, w in enumerate(words) if w["end"] <= latest + 1e-6]
    if below:
        tail = words[below[-1]]["end"]
        if target_end - tail <= tolerance + 1e-6:
            return tail
    return min(max(target_end, 0.0), duration)


def _choose_end_without_words(start: float, target_end: float, duration: float,
                              min_sec: float, tolerance: float,
                              silences: list[tuple[float, float]]) -> float:
    """Silence-based end selection when no word timestamps exist: land inside
    the silence nearest the target so we never cut during active speech."""
    earliest = start + max(1.0, min_sec - tolerance)
    latest = min(duration, target_end + tolerance)
    best_end: float | None = None
    best_dist = float("inf")
    for a, b in silences:
        a, b = max(0.0, a), min(duration, b)
        if b <= a:
            continue
        end = a + min(0.2, (b - a) / 2.0)  # keep the speech, take a tiny tail
        if earliest <= end <= latest:
            dist = abs(end - target_end)
            if dist < best_dist:
                best_dist, best_end = dist, end
    if best_end is not None:
        return best_end
    return min(max(target_end, 0.0), duration)


def resolve_clip_boundaries(
    candidates: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    duration: float,
    min_sec: float,
    tolerance: float | None = None,
    media_path: str | None = None,
) -> list[dict[str, Any]]:
    """Move every candidate's start/end onto natural speech boundaries.

    Deterministic: uses Whisper word-level timestamps when present, FFmpeg
    silence detection when the transcript carries no usable timestamps, and
    duration-based clamping only when neither is available. Candidates are
    updated in place (start/end rounded to 2 decimals) and returned.
    """
    tol = _clamp_tolerance(tolerance)
    duration = max(float(duration or 0.0), 0.0)
    if duration <= 0:
        return candidates
    words = _flatten_words(segments)
    silences: list[tuple[float, float]] | None = None

    def _get_silences() -> list[tuple[float, float]]:
        nonlocal silences
        if silences is None:
            silences = []
            if media_path:
                try:
                    from . import ffmpeg_engine
                    silences = ffmpeg_engine.detect_silences(media_path)
                except Exception as exc:
                    logger.warning("silence detection unavailable for %s: %s", media_path, exc)
        return silences

    for c in candidates:
        try:
            proposed_start = float(c["start"])
            proposed_end = float(c["end"])
        except (KeyError, TypeError, ValueError):
            continue
        target_end = min(max(proposed_end, proposed_start + 1.0), duration)
        if words:
            start = _snap_start(words, min(max(proposed_start, 0.0), duration), tol)
            start = min(max(start, 0.0), max(0.0, duration - 0.5))
            end = _choose_end(words, start, target_end, duration, min_sec, tol)
        else:
            start = min(max(proposed_start, 0.0), max(0.0, duration - 0.5))
            end = _choose_end_without_words(start, target_end, duration, min_sec, tol,
                                            _get_silences())
        if end <= start:
            end = min(duration, start + max(1.0, min_sec))
        start, end = round(start, 2), round(end, 2)
        if abs(start - proposed_start) > 0.01 or abs(end - proposed_end) > 0.01:
            logger.debug("Speech-aware boundary: %.2f-%.2f -> %.2f-%.2f",
                         proposed_start, proposed_end, start, end)
        c["start"], c["end"] = start, end
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


def dedupe_candidates(candidates: list[dict[str, Any]], overlap_threshold: float = 0.35,
                      use_similarity: bool = True) -> list[dict[str, Any]]:
    """Remove candidates that substantially overlap or repeat each other.

    ``use_similarity`` gates the excerpt word-set check. Keep it on for
    LLM-produced candidates (tight verbatim excerpts); turn it off for
    heuristic candidates, whose excerpts are ±4-segment context windows and
    would otherwise be flagged as near-identical despite being distinct
    moments."""
    ranked = sorted(candidates, key=lambda c: -c["score"])
    kept: list[dict[str, Any]] = []
    for cand in ranked:
        ok = True
        for k in kept:
            if _overlap(cand, k) > overlap_threshold:
                ok = False
                break
            if use_similarity and _similar(cand, k):
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
def _duration_fallback_candidates(
    duration: float,
    clip_count: int,
    min_sec: float,
    max_sec: float,
) -> list[dict[str, Any]]:
    """Anchor candidates when the transcript carries no usable content at all.
    Anchors are spread across the video and rely on silence detection (or
    plain duration clamping) for their final boundaries."""
    length = min(float(max_sec), float(duration))
    count = max(1, int(clip_count))
    if length <= 0:
        return []
    if duration <= length:
        starts = [0.0]
    elif count == 1:
        starts = [max(0.0, (duration - length) / 2.0)]
    else:
        step = (duration - length) / (count - 1)
        starts = [i * step for i in range(count)]
    return [
        {
            "start": round(st, 2),
            "end": round(min(duration, st + length), 2),
            "transcript_excerpt": "",
            "score": 5.0,
            "reason": "Selected by duration-based fallback (no transcript available).",
            "hook": "Watch this",
            "title": f"Clip {i + 1}",
            "description": "",
            "category": "",
        }
        for i, st in enumerate(starts)
    ]


def select_moments(
    video_title: str,
    duration: float,
    segments: list[dict[str, Any]],
    clip_count: int,
    min_sec: float,
    max_sec: float,
    quality: str = "balanced",
    media_path: str | None = None,
) -> dict[str, Any]:
    """Rank and select the best moments. Uses LM Studio when available,
    falls back to local heuristics (flagged) when it is not.

    `media_path` (optional) enables FFmpeg silence detection as a boundary
    fallback when the transcript carries no usable word timestamps.
    """
    transcript_text = segments_text(segments)
    if not transcript_text.strip():
        if media_path and duration and duration > 0:
            logger.info("Empty transcript for selection — using silence/duration fallback")
            candidates = _duration_fallback_candidates(duration, clip_count, min_sec, max_sec)
            candidates = resolve_clip_boundaries(candidates, segments, duration,
                                                 min_sec, media_path=media_path)
            if clip_count > 0:
                candidates = candidates[:clip_count]
            return {"candidates": candidates, "source": "heuristic",
                    "error": "Transcript is empty — silence/duration-based fallback used"}
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
        source = "heuristic"
    else:
        source = "lmstudio" if use_llm else "none"

    if candidates:
        # Move boundaries onto natural speech boundaries (deterministic word /
        # silence timestamps; never mid-word, never mid-thought when avoidable).
        # Re-dedupe only if some boundary actually moved — extensions can push
        # neighbours into overlap. Wordless transcripts leave boundaries
        # untouched, so legacy selection keeps its exact behaviour.
        before = [(c.get("start"), c.get("end")) for c in candidates]
        candidates = resolve_clip_boundaries(candidates, segments, duration,
                                             min_sec, media_path=media_path)
        if any((c.get("start"), c.get("end")) != b for c, b in zip(candidates, before)):
            # extensions can create footage overlap — drop only time-overlapping
            # neighbours; excerpt similarity would false-positive on heuristic
            # context-window excerpts
            candidates = dedupe_candidates(candidates, use_similarity=False)

    # final cap
    if clip_count > 0:
        candidates = sorted(candidates, key=lambda c: -c["score"])[:clip_count]

    return {"candidates": candidates, "source": source, "error": error if not use_llm else ""}
