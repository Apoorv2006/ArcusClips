"""Tests for the local Whisper transcription service (faster-whisper).

The model itself is not loaded (HuggingFace network is unavailable); these
tests exercise the error-handling contract and the source-hash reuse logic
with a stubbed model.
"""
import pytest

from app.services import whisper_service


def test_get_model_error_includes_original_exception(app_env, monkeypatch):
    """The load-failure message must include the real exception (not a literal)."""
    # clear any cached model
    whisper_service._model_cache.clear()

    class _BoomModel:
        def __init__(self, *a, **k):
            raise OSError("disk full while downloading weights")

    import sys
    import types

    fake_mod = types.ModuleType("faster_whisper")
    fake_mod.WhisperModel = _BoomModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_mod)

    with pytest.raises(RuntimeError) as exc:
        whisper_service._get_model()
    msg = str(exc.value)
    assert "Failed to load Whisper model" in msg
    # the original exception text must be present (regression: was a literal {exc})
    assert "disk full while downloading weights" in msg
    assert "{exc}" not in msg


def test_get_model_caches_by_key(app_env, monkeypatch):
    """A loaded model is cached so repeated calls don't re-instantiate."""
    whisper_service._model_cache.clear()
    created = {"n": 0}

    class _Model:
        pass

    class _Factory:
        def __init__(self, *a, **k):
            created["n"] += 1

    import sys
    import types

    fake_mod = types.ModuleType("faster_whisper")
    fake_mod.WhisperModel = _Factory
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_mod)

    # _get_model returns the model instance; our factory returns _Factory obj
    whisper_service._get_model()
    whisper_service._get_model()
    assert created["n"] == 1
    whisper_service._model_cache.clear()


def test_file_hash_is_deterministic(tmp_path):
    """file_hash returns a stable MD5 for identical content (reuse detection)."""
    p = tmp_path / "a.txt"
    p.write_bytes(b"hello world")
    h1 = whisper_service.file_hash(p)
    h2 = whisper_service.file_hash(p)
    assert h1 == h2
    assert len(h1) == 32
