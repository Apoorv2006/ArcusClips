"""Speech-aware clip boundary tests.

The duration preset is a *target*, not a hard limit: clips extend to (or end
at) a natural speech boundary — sentence-ending punctuation or a pause —
within a configurable tolerance. Cases covered:

1. 15s target, sentence ends at ~17s     -> extend to the natural ending
2. 15s target, speaker finishes at ~13s  -> end naturally, no padding
3. Target lands mid-sentence             -> move to the completion point
4. Target lands mid-word                -> never split a word
5. Continuous dialogue, no pause        -> best boundary within tolerance
6. No transcript / no timestamps        -> silence detection, else duration-based
7. Duration presets + caption sync      -> downstream stays in sync
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import ffmpeg_engine, selection


def _wseg(seg_index: int, words: list[tuple[str, float, float]]) -> dict:
    text = " ".join(w[0] for w in words)
    return {
        "start": words[0][1],
        "end": words[-1][2],
        "text": text,
        "words": [{"word": w[0], "start": w[1], "end": w[2]} for w in words],
        "word_count": len(words),
    }


def _sentence_words(prefix: str, start: float, gap: float = 0.25) -> list[tuple[str, float, float]]:
    """Words 0.35s wide separated by `gap`. Last word ends at
    start + (n-1)*(0.35+gap) + 0.35."""
    words, t = [], start
    stride = 0.35 + gap
    for i, tok in enumerate(prefix.split(" ")):
        s = round(t + i * stride, 3)
        words.append((tok, s, round(s + 0.35, 3)))
    return words


# 13-word sentence, gap 1.0s -> ends at 12*1.35 + 0.35 = 16.55s ("~17s")
CASE1_SENTENCE = "Today I want to talk about the exact process you need to follow."


def _case1_segments() -> list[dict]:
    return [_wseg(0, _sentence_words(CASE1_SENTENCE, 0.0, gap=1.0))]


def test_1_extends_to_natural_sentence_end_within_tolerance():
    segments = _case1_segments()
    last_end = segments[0]["words"][-1]["end"]
    assert last_end == pytest.approx(17.0, abs=0.5)
    cand = [{"start": 0.0, "end": 15.0}]
    out = selection.resolve_clip_boundaries(cand, segments, duration=60.0,
                                            min_sec=15.0, tolerance=4.0)
    assert out[0]["end"] == pytest.approx(last_end, abs=0.01)
    assert out[0]["end"] > 15.0  # extended past the hard cutoff


def test_2_ends_at_natural_completion_not_at_target():
    # 17 words, stride 0.79 -> speaker finishes at ~13s; nothing is said after
    words = _sentence_words(
        "here is the one thing you must understand about this whole topic today and I mean it",
        0.0, gap=0.44)
    speaker_done = words[-1][2]
    assert speaker_done == pytest.approx(13.0, abs=0.5)
    segments = [_wseg(0, words)]
    cand = [{"start": 0.0, "end": 15.0}]
    out = selection.resolve_clip_boundaries(cand, segments, duration=120.0,
                                            min_sec=15.0, tolerance=4.0)
    assert out[0]["end"] == pytest.approx(speaker_done, abs=0.01)
    assert out[0]["end"] < 15.0  # shorter complete thought beats the exact cut


def test_3_target_mid_sentence_moves_to_completion_point():
    # sentence completes at ~6.35s; target at 10s lands mid-sentence (past the
    # completion) — the resolver must pick the sentence-final boundary, which
    # is the nearest complete thought to the target inside the tolerance
    s1 = _sentence_words("This is the most important idea you will hear today honestly.",
                         0.0, gap=0.25)
    sent_end = s1[-1][2]
    segments = [_wseg(0, s1)]
    cand = [{"start": 0.0, "end": 10.0}]
    out = selection.resolve_clip_boundaries(cand, segments, duration=120.0,
                                            min_sec=8.0, tolerance=4.0)
    assert out[0]["end"] == pytest.approx(sent_end, abs=0.01)
    assert out[0]["end"] != pytest.approx(10.0, abs=0.01)


def test_4_never_splits_a_word():
    words = _sentence_words("the quick brown fox jumps over the lazy dog tonight", 0.0, gap=0.15)
    segments = [_wseg(0, words)]
    word_ends = {w["end"] for seg in segments for w in seg["words"]}
    word_starts = {w["start"] for seg in segments for w in seg["words"]}
    cand = [{"start": 0.31, "end": 3.13}]  # both land inside words
    out = selection.resolve_clip_boundaries(cand, segments, duration=60.0,
                                            min_sec=2.0, tolerance=2.0)
    assert out[0]["end"] in word_ends
    assert out[0]["start"] in word_starts
    for seg in segments:
        for w in seg["words"]:
            assert not (w["start"] < out[0]["end"] < w["end"])
            assert not (w["start"] < out[0]["start"] < w["end"])


def test_5_continuous_dialogue_picks_nearest_word_boundary_in_tolerance():
    # no punctuation, no gaps above the pause threshold: best available
    # (closest) word edge to the target inside the tolerance window
    words, t = [], 0.0
    for i in range(60):
        words.append((f"word{i}", round(t, 3), round(t + 0.38, 3)))
        t += 0.42  # uniform 0.04s gap — below every pause threshold
    segments = [_wseg(0, words)]
    cand = [{"start": 0.0, "end": 15.0}]
    out = selection.resolve_clip_boundaries(cand, segments, duration=60.0,
                                            min_sec=15.0, tolerance=4.0)
    word_ends = [round(w[2], 2) for w in words]
    assert out[0]["end"] in word_ends  # not mid-word
    assert 11.0 <= out[0]["end"] <= 19.0  # inside the tolerance window
    assert out[0]["end"] == pytest.approx(min(word_ends, key=lambda e: abs(e - 15.0)),
                                          abs=0.01)


def test_6a_no_transcript_uses_silence_detection(monkeypatch):
    calls = []

    def fake_detect(path, *a, **k):
        calls.append(path)
        return [(28.0, 30.5), (112.0, 115.0)]

    monkeypatch.setattr(ffmpeg_engine, "detect_silences", fake_detect)
    result = selection.select_moments("Silent video", 120.0, [], 2, 15.0, 30.0,
                                      "balanced", media_path="/fake/video.mp4")
    assert calls == ["/fake/video.mp4"]  # silence detection ran exactly once
    ends = sorted(c["end"] for c in result["candidates"])
    assert ends[0] == pytest.approx(28.2, abs=0.01)
    assert ends[1] == pytest.approx(112.2, abs=0.01)


def test_6b_silence_unavailable_falls_back_to_duration_based(monkeypatch):
    monkeypatch.setattr(ffmpeg_engine, "detect_silences", lambda *a, **k: [])
    result = selection.select_moments("Silent video", 120.0, [], 2, 15.0, 30.0,
                                      "balanced", media_path="/fake/video.mp4")
    assert result["candidates"]  # still produces clips
    for c in result["candidates"]:
        assert 0.0 <= c["start"] < c["end"] <= 120.0
        assert c["end"] - c["start"] <= 30.0


def test_6c_silence_detection_failure_is_tolerated(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("ffmpeg exploded")

    monkeypatch.setattr(ffmpeg_engine, "detect_silences", boom)
    result = selection.select_moments("Silent video", 90.0, [], 1, 15.0, 30.0,
                                      "balanced", media_path="/fake/video.mp4")
    assert result["candidates"]
    assert 0.0 <= result["candidates"][0]["start"] < result["candidates"][0]["end"] <= 90.0


def test_6d_no_media_path_keeps_duration_based_behavior():
    result = selection.select_moments("Silent video", 120.0, [], 2, 15.0, 30.0, "balanced")
    assert result["candidates"] == []  # empty transcript, nothing to hang off
    assert result["source"] == "empty"


def test_silence_parser_extracts_intervals(monkeypatch):
    fake_stderr = (
        "[silencedetect @ 0x55] silence_start: 1.5\n"
        "[silencedetect @ 0x55] silence_end: 3.25 | silence_duration: 1.75\n"
        "some unrelated noise\n"
        "[silencedetect @ 0x55] silence_start: 30.0\n"
        "[silencedetect @ 0x55] silence_end: 31.25 | silence_duration: 1.25\n"
        "[silencedetect @ 0x55] silence_start: 58.0\n"  # runs to EOF: ignored
    )
    monkeypatch.setattr(ffmpeg_engine, "_run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stderr=fake_stderr,
                                                        stdout=""))
    silences = ffmpeg_engine.detect_silences("/fake/video.mp4")
    assert silences == [(1.5, 3.25), (30.0, 31.25)]


def test_silence_fallback_avoids_cutting_during_active_speech(monkeypatch):
    # speech everywhere except 15.5-17.0 — the clip must end inside that silence
    monkeypatch.setattr(ffmpeg_engine, "detect_silences",
                        lambda *a, **k: [(15.5, 17.0)])
    cand = [{"start": 0.0, "end": 15.0}]
    out = selection.resolve_clip_boundaries(cand, [], duration=60.0,
                                            min_sec=15.0, tolerance=4.0,
                                            media_path="/fake/video.mp4")
    assert out[0]["end"] == pytest.approx(15.7, abs=0.01)


def test_tolerance_is_configurable_via_settings(monkeypatch):
    from app.config import get_settings

    settings_instance = get_settings()  # the proxy forwards to this instance
    words = _sentence_words("one two three four five six seven eight nine ten eleven twelve.",
                            0.0, gap=0.25)
    segments = [_wseg(0, words)]
    target = words[-1][2] - 3.0
    cand = [{"start": 0.0, "end": target}]
    # shrunk budget (1s): cannot reach the sentence end, stays near the target
    monkeypatch.setattr(settings_instance, "clip_duration_tolerance_seconds", 1.0)
    out = selection.resolve_clip_boundaries(cand, segments, duration=60.0, min_sec=1.0)
    assert abs(out[0]["end"] - target) <= 1.0 + 0.01
    # generous budget (5s): reaches the sentence end
    monkeypatch.setattr(settings_instance, "clip_duration_tolerance_seconds", 5.0)
    out = selection.resolve_clip_boundaries(cand, segments, duration=60.0, min_sec=1.0)
    assert out[0]["end"] == pytest.approx(words[-1][2], abs=0.01)


def test_does_not_pad_into_speech_after_completion():
    # speech ends at ~5.65s with nothing after it — clip closes there even
    # though the 25s target could legally extend much further
    words = _sentence_words("short answer. then we continue talking for a while.",
                            0.5, gap=0.25)
    speech_end = words[-1][2]  # 5.65
    segments = [_wseg(0, words)]
    cand = [{"start": 0.0, "end": 25.0}]
    out = selection.resolve_clip_boundaries(cand, segments, duration=60.0,
                                            min_sec=5.0, tolerance=4.0)
    assert out[0]["end"] <= speech_end + 0.01


def test_start_pulls_back_to_sentence_start_within_half_tolerance():
    words = _sentence_words("First thought. Second thought starts here now.",
                            0.0, gap=0.25)
    second_start = words[2][1]  # "Second"
    segments = [_wseg(0, words)]
    cand = [{"start": second_start + 0.5, "end": second_start + 9.0}]
    out = selection.resolve_clip_boundaries(cand, segments, duration=60.0,
                                            min_sec=5.0, tolerance=4.0)
    assert out[0]["start"] == pytest.approx(second_start, abs=0.01)


def test_7_caption_words_are_clamped_to_resolved_clip_window():
    segments = _case1_segments()
    all_words = selection._flatten_words(segments)
    cand = [{"start": 0.0, "end": 15.0}]
    out = selection.resolve_clip_boundaries(cand, segments, duration=60.0,
                                            min_sec=15.0, tolerance=4.0)
    cs, ce = out[0]["start"], out[0]["end"]
    caption_words = ffmpeg_engine._words_from_segments(segments, cs, ce)
    assert caption_words
    for _text, ws, we in caption_words:
        assert ws >= cs - 1e-6
        assert we <= ce + 1e-6
    # every word fully inside the resolved window is captioned — no gaps
    inside = [w for w in all_words if w["start"] >= cs - 1e-6 and w["end"] <= ce + 1e-6]
    assert len(caption_words) == len(inside)


def test_7_presets_unchanged_when_transcript_has_no_timestamps():
    # word-less transcripts keep duration-based boundaries (legacy behaviour)
    segments = [
        {"start": 0.0, "end": 4.0, "text": "First thought."},
        {"start": 4.0, "end": 9.0, "text": "Second thought."},
        {"start": 9.0, "end": 14.0, "text": "Third thought."},
    ]
    cand = [{"start": 0.0, "end": 10.0}]
    out = selection.resolve_clip_boundaries(cand, segments, duration=60.0,
                                            min_sec=5.0, tolerance=4.0)
    assert out[0]["start"] == pytest.approx(0.0, abs=0.01)
    assert out[0]["end"] == pytest.approx(10.0, abs=0.01)


def test_select_moments_end_to_end_extends_and_keeps_word_edges(monkeypatch):
    monkeypatch.setattr(selection.lmstudio, "is_available", lambda: False)
    segments = _case1_segments()
    result = selection.select_moments("Demo", 60.0, segments, 1, 5.0, 8.0,
                                      "balanced", media_path=None)
    assert result["source"] == "heuristic"
    assert len(result["candidates"]) == 1
    clip = result["candidates"][0]
    word_ends = {round(w["end"], 2) for seg in segments for w in seg["words"]}
    assert round(clip["end"], 2) in word_ends  # resolved onto a word edge


def _semi_sparse_segments():
    """Like FAKE_SEGMENTS in the e2e suite: only the opening has word times."""
    return [
        {"start": 0.0, "end": 4.0, "text": "Intro words here now.",
         "words": [{"word": "Intro", "start": 0.0, "end": 1.0},
                   {"word": "words", "start": 1.0, "end": 2.0}],
         "word_count": 2},
        {"start": 4.0, "end": 30.0, "text": "Long stretch with no word timestamps at all.",
         "words": [], "word_count": 0},
    ]


def test_sparse_word_timestamps_do_not_collapse_clip():
    cand = [{"start": 8.0, "end": 20.0}]
    out = selection.resolve_clip_boundaries(cand, _semi_sparse_segments(), duration=60.0,
                                            min_sec=8.0, tolerance=4.0)
    assert out[0]["end"] == pytest.approx(20.0, abs=0.01)  # duration-based, not 2.0


def test_transcript_tail_within_tolerance_is_trusted():
    # words stop at ~13s (within tolerance of the 15s target) — speech is over
    words = _sentence_words(
        "here is the one thing you must understand about this whole topic today and I mean it",
        0.0, gap=0.44)
    segments = [_wseg(0, words)]
    cand = [{"start": 0.0, "end": 15.0}]
    out = selection.resolve_clip_boundaries(cand, segments, duration=120.0,
                                            min_sec=15.0, tolerance=4.0)
    assert out[0]["end"] == pytest.approx(words[-1][2], abs=0.01)


def test_extension_beyond_tolerance_is_bounded():
    # sentence completes far beyond the tolerance budget — stop inside it
    words = _sentence_words(
        "aaa bbb ccc ddd eee fff ggg hhh iii jjj kkk lll mmm nnn ooo ppp qqq rrr sss ttt uuu vvv.",
        0.0, gap=0.5)
    segments = [_wseg(0, words)]
    cand = [{"start": 0.0, "end": 15.0}]
    out = selection.resolve_clip_boundaries(cand, segments, duration=120.0,
                                            min_sec=15.0, tolerance=2.0)
    assert out[0]["end"] <= 15.0 + 2.0 + 0.01
    word_ends = {w[2] for w in words}
    assert out[0]["end"] in word_ends
