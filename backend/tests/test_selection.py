"""Tests for moment selection: validation, dedup, heuristics, LLM path."""
from app.services import selection


SEGMENTS = [
    {"start": 0.0, "end": 4.0, "text": "Today we are going to talk about something amazing."},
    {"start": 4.0, "end": 9.0, "text": "Why is this important for you to understand?"},
    {"start": 9.0, "end": 14.0, "text": "The truth is most people never figure this out."},
    {"start": 14.0, "end": 20.0, "text": "Here is the exact process you need to follow."},
    {"start": 20.0, "end": 26.0, "text": "I think this will change how you work entirely."},
    {"start": 26.0, "end": 32.0, "text": "Let me show you an incredible example right now."},
]


def test_validate_rejects_out_of_range():
    candidates = [
        {"start": -5.0, "end": 10.0, "transcript_excerpt": "Today we are going", "score": 9},
        {"start": 0.0, "end": 999.0, "transcript_excerpt": "Today we are going", "score": 9},
        {"start": 1.0, "end": 16.0, "transcript_excerpt": "no such words here", "score": 9},
        {"start": 1.0, "end": 16.0, "transcript_excerpt": "Today we are going", "score": 9},
    ]
    valid = selection.validate_candidates(candidates, 60.0, SEGMENTS, 15.0, 30.0)
    assert len(valid) == 1
    assert valid[0]["start"] == 1.0


def test_validate_rejects_bad_duration():
    candidates = [{"start": 2.0, "end": 2.5, "transcript_excerpt": "Today we", "score": 9}]
    assert selection.validate_candidates(candidates, 60.0, SEGMENTS, 15.0, 30.0) == []


def test_validate_accepts_valid():
    candidates = [{"start": 0.0, "end": 18.0, "transcript_excerpt": "Today we are going to talk", "score": 8.5}]
    valid = selection.validate_candidates(candidates, 60.0, SEGMENTS, 15.0, 30.0)
    assert len(valid) == 1
    assert valid[0]["score"] == 8.5


def test_snap_to_segments():
    cands = [{"start": 1.2, "end": 17.7, "transcript_excerpt": "x"}]
    snapped = selection.snap_to_segments(cands, SEGMENTS)
    assert snapped[0]["start"] == 0.0
    assert snapped[0]["end"] == 20.0


def test_dedupe_overlap_and_similarity():
    cands = [
        {"start": 0.0, "end": 20.0, "transcript_excerpt": "Today we are going to talk about something amazing", "score": 9},
        {"start": 2.0, "end": 18.0, "transcript_excerpt": "Today we are going to talk about something amazing", "score": 8},
        {"start": 24.0, "end": 30.0, "transcript_excerpt": "Let me show you an incredible example right now", "score": 7},
    ]
    kept = selection.dedupe_candidates(cands)
    assert len(kept) == 2
    assert kept[0]["score"] == 9


def test_heuristic_candidates():
    result = selection.heuristic_candidates(" ".join(s["text"] for s in SEGMENTS), SEGMENTS,
                                           32.0, 2, 10.0, 15.0)
    assert len(result) == 2
    for c in result:
        assert 0 <= c["start"] < c["end"] <= 32.0
        assert 10.0 <= c["end"] - c["start"] <= 15.0


def test_select_moments_heuristic_fallback(app_env, monkeypatch):
    # LM Studio unavailable -> deterministic fallback, flagged
    monkeypatch.setattr(selection.lmstudio, "is_available", lambda: False)
    long_segments = SEGMENTS + [
        {**s, "start": s["start"] + 32.0, "end": s["end"] + 32.0} for s in SEGMENTS
    ]
    result = selection.select_moments("Test Video", 64.0, long_segments, 3, 5.0, 8.0, "balanced")
    assert result["source"] == "heuristic"
    assert len(result["candidates"]) == 3
    assert all(c["reason"] for c in result["candidates"])


def test_select_moments_lmstudio_path(app_env, monkeypatch):
    def fake_chat_json(messages, model=None, temperature=None):
        return {"candidates": [
            {"start": 0.0, "end": 18.0, "transcript_excerpt": "Today we are going to talk about something",
             "score": 9, "reason": "strong opening", "hook": "Watch this", "title": "The Truth",
             "description": "A great clip", "category": "education"},
            {"start": 14.0, "end": 32.0,
             "transcript_excerpt": "Here is the exact process you need to follow. I think this will change",
             "score": 8, "reason": "strong statement", "hook": "See this", "title": "Example",
             "description": "demo", "category": "education"},
        ]}

    monkeypatch.setattr(selection.lmstudio, "is_available", lambda: True)
    monkeypatch.setattr(selection.lmstudio, "chat_json", fake_chat_json)
    result = selection.select_moments("Test Video", 60.0, SEGMENTS, 2, 15.0, 30.0, "strict")
    assert result["source"] == "lmstudio"
    assert len(result["candidates"]) == 2
    assert result["candidates"][0]["score"] == 9


def test_select_moments_llm_failure_falls_back(app_env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("LM Studio exploded")

    monkeypatch.setattr(selection.lmstudio, "is_available", lambda: True)
    monkeypatch.setattr(selection.lmstudio, "chat_json", boom)
    result = selection.select_moments("Test Video", 60.0, SEGMENTS, 1, 15.0, 30.0, "balanced")
    assert result["source"] == "heuristic"
    assert len(result["candidates"]) == 1


def test_select_moments_empty_transcript():
    result = selection.select_moments("Empty", 60.0, [], 3, 15.0, 30.0)
    assert result["candidates"] == []
