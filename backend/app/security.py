"""Security utilities: credential encryption, password hashing, path safety,
SSRF guards and filename sanitization."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
from pathlib import Path
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken

from .config import settings

logger = logging.getLogger("arcusclips.security")

_FERNET: Fernet | None = None


# ---------------------------------------------------------------------------
# Credential encryption (Google OAuth tokens at rest)
# ---------------------------------------------------------------------------
def _load_fernet() -> Fernet:
    global _FERNET
    if _FERNET is not None:
        return _FERNET
    key = settings.oauth_credential_encryption_key.strip()
    if not key:
        # Auto-generate and persist a key so encrypted tokens survive restarts.
        key_file = settings.data_path / "oauth_credential.key"
        key_file.parent.mkdir(parents=True, exist_ok=True)
        if key_file.exists():
            key = key_file.read_text(encoding="utf-8").strip()
        else:
            key = Fernet.generate_key().decode("ascii")
            key_file.write_text(key, encoding="utf-8")
            try:
                os.chmod(key_file, 0o600)
            except OSError:
                pass
    _FERNET = Fernet(key.encode("ascii") if isinstance(key, str) else key)
    return _FERNET


def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _load_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    try:
        return _load_fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken:
        logger.error("Failed to decrypt credential — encryption key may have changed.")
        return ""


# ---------------------------------------------------------------------------
# Password hashing (PBKDF2-HMAC-SHA256, no external dependency)
# ---------------------------------------------------------------------------
_PBKDF2_ITERATIONS = 200_000


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), _PBKDF2_ITERATIONS
    ).hex()
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, digest = stored.split("$")
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iterations)
        ).hex()
        return hmac.compare_digest(candidate, digest)
    except (ValueError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# Path traversal prevention
# ---------------------------------------------------------------------------
_UNSAFE_CHARS = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")


def safe_filename(name: str, fallback: str = "file", max_length: int = 180) -> str:
    """Return a filename safe for the local filesystem (no traversal, no unsafe chars)."""
    name = _UNSAFE_CHARS.sub("_", name).strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    if not name:
        name = fallback
    if len(name) > max_length:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 12:
            name = stem[: max_length - len(ext) - 1] + "." + ext
        else:
            name = name[:max_length]
    return name


def safe_relative_path(rel: str) -> str:
    """Normalize a user-supplied relative path and reject traversal attempts."""
    rel = (rel or "").replace("\\", "/").strip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError("Path traversal is not allowed")
    return "/".join(parts)


def ensure_inside(base_dir: Path, target: Path) -> Path:
    """Assert that ``target`` resolves inside ``base_dir`` (symlink-safe)."""
    base = base_dir.resolve()
    resolved = target.resolve()
    if base != resolved and base not in resolved.parents:
        raise ValueError(f"Path escapes allowed directory: {target}")
    return resolved


# ---------------------------------------------------------------------------
# SSRF protection for user-submitted URLs
# ---------------------------------------------------------------------------
_BLOCKED_HOSTNAMES = {
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-allnodes",
    "metadata", "metadata.google.internal",
}
_BLOCKED_SUFFIXES = (".local", ".internal", ".localhost")


def is_public_ip(ip_str: str) -> bool:
    """True when ``ip_str`` is a publicly routable, non-special IP address.

    Loopback, private, link-local, multicast, reserved, unspecified and other
    non-global destinations (including IPv4-mapped IPv6 forms such as
    ``::ffff:127.0.0.1``) are rejected.
    """
    try:
        addr = ipaddress.ip_address((ip_str or "").split("%")[0])
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
        or not addr.is_global
    )


def resolve_public_addrinfo(host: str, port: int | None):
    """Resolve ``host`` and return raw ``getaddrinfo`` results, validating that
    EVERY resolved destination is public.

    Raises ``ValueError`` when resolution fails or any resolved address is a
    prohibited destination (mixed public/private answers are rejected too).
    The returned list is what a connecting socket should use — resolving once
    and connecting to the enumerated results avoids a DNS time-of-check /
    time-of-use gap and DNS rebinding between validation and connect.
    """
    try:
        infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"Could not resolve host: {exc}") from exc
    if not infos:
        raise ValueError("Could not resolve host: empty resolution result")
    for info in infos:
        ip = info[4][0]
        if not is_public_ip(ip):
            raise ValueError(f"Access to non-public address {ip} is not allowed")
    return infos


def check_hostname(host: str) -> str:
    """Validate a bare hostname (not IP literal resolution). Raises ValueError."""
    lowered = (host or "").lower().strip(".")
    if not lowered:
        raise ValueError("URL has no host")
    if lowered in _BLOCKED_HOSTNAMES or lowered.endswith(_BLOCKED_SUFFIXES):
        raise ValueError("Access to this host is not allowed")
    return lowered


def validate_public_url(url: str, allowed_domains: list[str] | None = None) -> str:
    """Validate a URL for fetching. Blocks private/loopback ranges and (optionally)
    enforces a domain allowlist. Returns the normalized URL or raises ValueError."""
    url = (url or "").strip()
    if not url:
        raise ValueError("URL is required")
    parsed = urlparse(url if "://" in url else "https://" + url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only http(s) URLs are allowed")
    host = parsed.hostname or ""
    lowered = check_hostname(host)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    # Resolve and reject private/loopback/link-local IPs (all of them).
    resolve_public_addrinfo(lowered, port)

    if allowed_domains:
        allowed = {d.lower().lstrip(".") for d in allowed_domains if d.strip()}
        if allowed and lowered not in allowed and not any(
            lowered.endswith("." + d) for d in allowed
        ):
            raise ValueError(f"Domain not in allowlist: {lowered}")
    return url


# ---------------------------------------------------------------------------
# Log redaction
# ---------------------------------------------------------------------------
def redact_secrets(text: str) -> str:
    """Redact likely secrets from log text."""
    text = re.sub(r"(api[_-]?key|token|secret|password)\s*[=:]\s*\S+", r"\1=***", text, flags=re.I)
    return text


def serialize_error(exc: BaseException) -> str:
    return redact_secrets(f"{type(exc).__name__}: {exc}")


def json_dumps(data) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)
