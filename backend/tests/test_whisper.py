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


def test_transcribe_audio_falls_back_to_ffmpeg_when_av_too_old(tmp_path, monkeypatch):
    """Simulate PyAV < 11 (the 'metadata_errors' TypeError from faster-whisper's
    decode_audio): transcription must still succeed using FFmpeg-decoded samples
    instead of crashing the job."""
    import subprocess
    import types

    import numpy as np

    from app.services import ffmpeg_engine

    wav = tmp_path / "tone.wav"
    subprocess.run(
        [ffmpeg_engine.get_ffmpeg_path(), "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-ac", "1", "-ar", "16000", str(wav)],
        capture_output=True, check=True,
    )

    seen: list[str] = []

    class _Info:
        language = "en"
        language_probability = 0.99
        duration = 1.0

    class _Word:
        def __init__(self, word, start, end):
            self.word = word
            self.start = start
            self.end = end

    class _Seg:
        start, end, text = 0.0, 1.0, " hello world"
        words = [_Word(" hello", 0.0, 0.5), _Word(" world", 0.5, 1.0)]

    class _Model:
        def transcribe(self, audio, **kwargs):
            seen.append(type(audio).__name__)
            if isinstance(audio, str):
                # what faster-whisper does with PyAV < 11 installed
                raise TypeError("open() got an unexpected keyword argument 'metadata_errors'")
            assert isinstance(audio, np.ndarray)
            assert audio.dtype == np.float32 and audio.ndim == 1
            assert kwargs["word_timestamps"] is True and kwargs["vad_filter"] is True
            return iter([_Seg()]), _Info()

    monkeypatch.setattr(whisper_service, "_get_model", lambda: _Model())

    result = whisper_service.transcribe_audio(str(wav))
    assert seen == ["str", "ndarray"]  # retried with decoded samples
    assert result["language"] == "en"
    assert result["duration"] == 1.0
    assert result["segments"][0]["word_count"] == 2
    assert result["segments"][0]["text"] == "hello world"


def test_transcribe_audio_reraises_unrelated_type_error(tmp_path, monkeypatch):
    """TypeErrors not coming from the av.open(metadata_errors) incompatibility
    must propagate — the fallback must not mask them."""
    from pathlib import Path as _Path

    wav = tmp_path / "tone.wav"
    wav.write_bytes(b"RIFF" + b"\0" * 32)

    class _Model:
        def transcribe(self, audio, **kwargs):
            raise TypeError("bad argument for some other reason")

    monkeypatch.setattr(whisper_service, "_get_model", lambda: _Model())

    with pytest.raises(TypeError, match="some other reason"):
        whisper_service.transcribe_audio(str(wav))
