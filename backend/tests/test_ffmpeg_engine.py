"""Tests for the FFmpeg/FFprobe engine: probing, ASS captions, rendering."""
import subprocess

import pytest

from app.services import ffmpeg_engine as fe


def test_probe_detects_streams(sample_video):
    info = fe.probe(sample_video)
    assert info.valid
    assert info.has_video and info.has_audio
    assert info.width == 640 and info.height == 360
    assert abs(info.duration - 60.0) < 1.0
    assert info.fps > 0


def test_probe_missing_file():
    info = fe.probe("/nonexistent/file.mp4")
    assert not info.valid
    assert info.detail


def test_ass_generation_all_styles(tmp_path, app_env):
    words = [("Hello", 0.0, 0.5), ("world", 0.5, 1.0)]
    for style in fe.CAPTION_STYLES:
        out = tmp_path / f"{style}.ass"
        fe.generate_ass(str(out), words, style, handle="@test", hook="Hook!")
        text = out.read_text()
        assert "[Script Info]" in text
        assert "[V4+ Styles]" in text
        assert style in text
        assert "Dialogue:" in text


def test_words_from_segments_word_level():
    segments = [
        {"start": 0.0, "end": 2.0, "text": "hello world",
         "words": [{"word": "hello", "start": 0.0, "end": 1.0},
                   {"word": "world", "start": 1.0, "end": 2.0}], "word_count": 2},
    ]
    words = fe._words_from_segments(segments, 0.0, 2.0)
    assert [w[0] for w in words] == ["hello", "world"]


def test_words_from_segments_fallback_split():
    segments = [{"start": 0.0, "end": 2.0, "text": "one two three", "words": [], "word_count": 0}]
    words = fe._words_from_segments(segments, 0.0, 2.0)
    assert len(words) == 3
    assert words[0][1] == pytest.approx(0.0, abs=0.01)


def test_extract_audio(sample_video, tmp_path):
    wav = str(tmp_path / "audio.wav")
    fe.extract_audio(sample_video, wav)
    info = fe.probe(wav)
    assert info.has_audio


@pytest.mark.parametrize("style", fe.CAPTION_STYLES)
def test_render_all_caption_styles(sample_video, tmp_path, style):
    out = str(tmp_path / f"{style}.mp4")
    words = [("This", 0.0, 0.4), ("is", 0.4, 0.8), ("a", 0.8, 1.2), ("test", 1.2, 1.8)]
    result = fe.render_clip(sample_video, 2.0, 8.0, out, style=style, layout="blur",
                            handle="@test", hook="Watch", words=words)
    assert result["ok"], result.get("error")
    rendered = fe.probe(out)
    assert rendered.valid
    assert rendered.width == fe.settings.render_width
    assert rendered.height == fe.settings.render_height


@pytest.mark.parametrize("layout", fe.LAYOUTS)
def test_render_all_layouts(sample_video, tmp_path, layout):
    out = str(tmp_path / f"{layout}.mp4")
    words = [("layout", 0.0, 1.0), ("test", 1.0, 2.0)]
    result = fe.render_clip(sample_video, 2.0, 8.0, out, style="clean", layout=layout, words=words)
    assert result["ok"], result.get("error")
    rendered = fe.probe(out)
    assert rendered.valid
    assert abs(rendered.duration - 6.0) < 1.0


def test_render_without_audio_source(tmp_path, app_env):
    ff = fe.get_ffmpeg_path()
    src = str(tmp_path / "noaudio.mp4")
    subprocess.run(
        [ff, "-y", "-f", "lavfi", "-i", "testsrc=duration=10:size=640x360:rate=30",
         "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p", src],
        capture_output=True, check=True,
    )
    out = str(tmp_path / "clip.mp4")
    result = fe.render_clip(src, 1.0, 5.0, out, style="bounce", layout="full",
                            words=[("no", 0.0, 1.0), ("audio", 1.0, 2.0)])
    assert result["ok"], result.get("error")


def test_thumbnail(sample_video, tmp_path):
    out = str(tmp_path / "thumb.jpg")
    fe.make_thumbnail(sample_video, 30.0, out)
    assert (tmp_path / "thumb.jpg").exists()
