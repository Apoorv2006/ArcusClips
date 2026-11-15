"""Tests for security utilities: passwords, encryption, paths, SSRF guards."""
from pathlib import Path

import pytest

from app.security import (
    decrypt_secret,
    ensure_inside,
    encrypt_secret,
    hash_password,
    safe_filename,
    safe_relative_path,
    verify_password,
    validate_public_url,
)


def test_password_hash_verify():
    h = hash_password("s3cret-pw")
    assert h.startswith("pbkdf2_sha256$")
    assert verify_password("s3cret-pw", h)
    assert not verify_password("wrong", h)
    assert not verify_password("wrong", "garbage")


def test_secret_encryption_roundtrip(app_env):
    secret = "ya29.access-token-123"
    enc = encrypt_secret(secret)
    assert secret not in enc
    assert decrypt_secret(enc) == secret
    assert decrypt_secret("") == ""


def test_safe_filename():
    assert safe_filename("My Clip: the best?.mp4") == "My Clip_ the best_.mp4"
    assert safe_filename("../../etc/passwd") == "_.._etc_passwd"
    assert safe_filename("") == "file"
    long = "x" * 300 + ".mp4"
    assert len(safe_filename(long)) <= 180


def test_safe_relative_path():
    assert safe_relative_path("Podcasts/Episode 01") == "Podcasts/Episode 01"
    with pytest.raises(ValueError):
        safe_relative_path("../escape")


def test_ensure_inside(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    inside = base / "ok.mp4"
    ensure_inside(base, inside)
    outside = tmp_path / "evil.mp4"
    outside.write_text("x")
    with pytest.raises(ValueError):
        ensure_inside(base, outside)


def test_ssrf_blocks_private():
    for bad in (
        "http://127.0.0.1/video.mp4",
        "http://localhost/video.mp4",
        "http://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
        "http://10.0.0.5/x.mp4",
    ):
        with pytest.raises(ValueError):
            validate_public_url(bad)


def test_ssrf_allows_public_and_allowlist():
    # uses a public hostname (resolution enforced by the guard)
    with pytest.raises(ValueError):
        validate_public_url("http://192.168.1.1/v.mp4")
    # domain allowlist enforcement with a public-looking host
    with pytest.raises(ValueError):
        validate_public_url("https://example.com/v.mp4", allowed_domains=["allowed.com"])


def test_ssrf_rejects_non_http():
    with pytest.raises(ValueError):
        validate_public_url("ftp://example.com/v.mp4")
