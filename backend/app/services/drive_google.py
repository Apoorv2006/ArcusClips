"""Google Drive integration — Google Drive API v3 + OAuth 2.0.

Read-only access by default (``drive.readonly`` scope). Tokens are stored
encrypted at rest. The recursive crawler handles pagination, shared drives,
shortcuts, dedup, permission failures and interrupted scans. Downloads stream
to disk and never load full files into memory.
"""
from __future__ import annotations

import io
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

from ..config import settings
from ..security import decrypt_secret, encrypt_secret

logger = logging.getLogger("arcusclips.drive")

ProgressCb = Callable[[float, str], None]

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

VIDEO_MIME_HINTS = (
    "video/mp4", "video/quicktime", "video/x-matroska", "video/webm",
    "video/x-msvideo", "video/mpeg", "video/mp2t", "video/ogg",
)


class DriveError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------
def build_auth_url(state: str) -> str:
    params = {
        "client_id": settings.google_oauth_client_id,
        "redirect_uri": settings.google_oauth_redirect_uri,
        "response_type": "code",
        "scope": settings.google_drive_scope,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str) -> dict[str, Any]:
    import requests

    resp = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret,
            "redirect_uri": settings.google_oauth_redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=60,
    )
    if resp.status_code != 200:
        raise DriveError(f"Token exchange failed: HTTP {resp.status_code} {resp.text[:200]}")
    return resp.json()


def refresh_tokens(refresh_token: str) -> dict[str, Any]:
    import requests

    resp = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "client_id": settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=60,
    )
    if resp.status_code != 200:
        raise DriveError(f"Token refresh failed: HTTP {resp.status_code} {resp.text[:200]}")
    return resp.json()


def oauth_configured() -> bool:
    return bool(settings.google_oauth_client_id and settings.google_oauth_client_secret)


# ---------------------------------------------------------------------------
# Drive client
# ---------------------------------------------------------------------------
def _authorized_client(access_token: str, refresh_token: str | None = None):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri=GOOGLE_TOKEN_URL,
        client_id=settings.google_oauth_client_id,
        client_secret=settings.google_oauth_client_secret,
        scopes=[settings.google_drive_scope],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def client_from_connection(conn) -> Any:
    """Build an authorized Drive client from a stored (encrypted) connection row."""
    access = decrypt_secret(conn.encrypted_access_token or "")
    refresh = decrypt_secret(conn.encrypted_refresh_token or "") or None
    if not access:
        raise DriveError("No stored Google Drive credentials")
    client = _authorized_client(access, refresh)
    try:
        about = client.about().get(fields="user").execute()
        return client, about.get("user", {})
    except Exception as exc:
        if refresh and ("401" in str(exc) or "invalid_token" in str(exc) or "expired" in str(exc).lower()):
            new = refresh_tokens(refresh)
            access = new.get("access_token", access)
            refresh = new.get("refresh_token", refresh)
            conn.encrypted_access_token = encrypt_secret(access)
            conn.encrypted_refresh_token = encrypt_secret(refresh or "")
            from ..db import utcnow

            conn.token_expires_at = None
            client = _authorized_client(access, refresh)
            about = client.about().get(fields="user").execute()
            return client, about.get("user", {})
        raise DriveError(f"Drive API error: {exc}") from exc


def store_tokens(conn, tokens: dict[str, Any]) -> None:
    conn.encrypted_access_token = encrypt_secret(tokens.get("access_token", ""))
    conn.encrypted_refresh_token = encrypt_secret(tokens.get("refresh_token", ""))
    from ..db import utcnow

    expires = tokens.get("expires_in")
    if expires:
        from datetime import timedelta

        conn.token_expires_at = utcnow() + timedelta(seconds=int(expires))


# ---------------------------------------------------------------------------
# File classification
# ---------------------------------------------------------------------------
def is_video_file(mime_type: str, name: str) -> bool:
    mime = (mime_type or "").lower()
    suffix = Path(name or "").suffix.lower().lstrip(".")
    if mime.startswith("video/") or any(h in mime for h in VIDEO_MIME_HINTS):
        return True
    return suffix in settings.supported_video_suffixes


def _fmt_modified(value: str | None):
    if not value:
        return None
    from datetime import datetime, timezone

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        return None


def _file_entry(f: dict[str, Any]) -> dict[str, Any]:
    is_shortcut = f.get("mimeType") == "application/vnd.google-apps.shortcut"
    targets = []
    if is_shortcut and f.get("shortcutDetails", {}).get("targetId"):
        targets.append(f["shortcutDetails"]["targetId"])
    return {
        "id": f["id"],
        "name": f.get("name", ""),
        "mime_type": f.get("mimeType", ""),
        "size": int(f.get("size", 0) or 0),
        "modified_at": _fmt_modified(f.get("modifiedTime")),
        "parent_id": (f.get("parents") or [""])[0],
        "is_shortcut": is_shortcut,
        "downloadable": bool(f.get("downloadUrl")) or not f.get("mimeType", "").startswith(
            "application/vnd.google-apps"
        ),
        "checksum": f.get("md5Checksum", "") or "",
        "version": str(f.get("version", "") or ""),
        "shortcut_targets": targets,
    }


# ---------------------------------------------------------------------------
# Browsing
# ---------------------------------------------------------------------------
def list_roots(client) -> list[dict[str, Any]]:
    """My Drive root + shared drives."""
    roots: list[dict[str, Any]] = [
        {"id": "root", "name": "My Drive", "kind": "folder", "is_root": True}
    ]
    try:
        drives = client.drives().list(pageSize=100).execute()
        for d in drives.get("drives", []):
            roots.append({"id": d["id"], "name": d.get("name", ""), "kind": "drive", "is_root": True})
    except Exception as exc:
        logger.warning("Shared drives listing failed: %s", exc)
    return roots


def list_children(client, folder_id: str) -> list[dict[str, Any]]:
    """List children of a folder (or root) with pagination."""
    items: list[dict[str, Any]] = []
    page_token = ""
    while True:
        params: dict[str, Any] = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "pageSize": 1000,
            "fields": (
                "nextPageToken, files(id,name,mimeType,size,modifiedTime,parents,"
                "downloadUrl,md5Checksum,version,shortcutDetails)"
            ),
            "orderBy": "folder,name",
        }
        if page_token:
            params["pageToken"] = page_token
        try:
            resp = client.files().list(**params).execute()
        except Exception as exc:
            raise DriveError(f"Failed listing folder {folder_id}: {exc}") from exc
        files = resp.get("files", [])
        items.extend(_file_entry(f) for f in files)
        page_token = resp.get("nextPageToken", "")
        if not page_token:
            break
    return items


def get_file_meta(client, file_id: str) -> dict[str, Any]:
    f = client.files().get(
        fileId=file_id,
        fields="id,name,mimeType,size,modifiedTime,parents,downloadUrl,md5Checksum,version,shortcutDetails",
    ).execute()
    return _file_entry(f)


# ---------------------------------------------------------------------------
# Recursive scan
# ---------------------------------------------------------------------------
def scan_folders(
    client,
    folder_ids: list[str],
    exclude_ids: list[str] | None = None,
    progress_cb: ProgressCb | None = None,
    max_files: int = 10000,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Recursively crawl folders. Returns (video_files, stats).

    Handles: multiple roots, pagination, shared drives, shortcuts (resolved
    safely without circular traversal), permission failures, dedup, and
    visited-folder tracking.
    """
    cb = progress_cb or (lambda p, m: None)
    exclude = set(exclude_ids or [])
    visited: set[str] = set()
    videos: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    stats = {"visited": 0, "files": 0, "folders": 0, "errors": 0, "skipped": 0}

    queue: list[tuple[str, str]] = []  # (folder_id, relative_path)
    for fid in folder_ids:
        if fid in exclude:
            continue
        queue.append((fid, ""))

    while queue:
        folder_id, rel_path = queue.pop(0)
        if folder_id in visited or folder_id in exclude:
            continue
        visited.add(folder_id)
        cb(0.0, f"Scanning folder: {rel_path or folder_id}")
        try:
            children = list_children(client, folder_id)
        except DriveError as exc:
            stats["errors"] += 1
            errors[folder_id] = str(exc)
            logger.warning("Folder %s scan error: %s", folder_id, exc)
            continue

        for child in children:
            stats["files"] += 1
            cid = child["id"]
            if child["is_shortcut"]:
                # resolve shortcuts safely: the visited set prevents cycles
                for target in child.get("shortcut_targets", []):
                    if target not in visited and target not in exclude:
                        queue.append((target, rel_path))
                stats["skipped"] += 1
                continue
            if is_video_file(child["mime_type"], child["name"]):
                if cid not in videos:
                    video = dict(child)
                    video["relative_path"] = "/".join(
                        p for p in ([rel_path, child["name"]] if rel_path else [child["name"]]) if p
                    )
                    videos[cid] = video
            else:
                if child["mime_type"] == "application/vnd.google-apps.folder":
                    stats["folders"] += 1
                    queue.append((cid, f"{rel_path}/{child['name']}" if rel_path else child["name"]))
        cb(
            min(0.99, len(visited) / max(1, len(visited) + len(queue))),
            f"Visited {len(visited)} folders, {len(videos)} videos",
        )
        if len(videos) >= max_files:
            logger.warning("Reached max_files=%d during scan; stopping early", max_files)
            break

    result = sorted(videos.values(), key=lambda v: v.get("name", ""))
    stats["videos"] = len(result)
    stats["visited_folders"] = len(visited)
    cb(1.0, f"Scan complete: {len(result)} videos")
    return result, stats


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------
def download_file(
    client,
    file_id: str,
    out_path: str | Path,
    file_size: int = 0,
    progress_cb: ProgressCb | None = None,
    max_retries: int = 3,
    cancel_event=None,
) -> str:
    """Stream a file from Drive to disk with retry/backoff and size limits."""
    cb = progress_cb or (lambda p, m: None)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".part")

    if file_size and file_size > settings.max_download_size:
        raise DriveError(
            f"File exceeds max download size ({file_size} > {settings.max_download_size})"
        )

    attempt = 0
    while True:
        if cancel_event is not None and cancel_event.is_set():
            tmp_path.unlink(missing_ok=True)
            raise DriveError("Download cancelled")
        try:
            _stream_download(client, file_id, tmp_path, file_size, cb)
            tmp_path.replace(out_path)
            cb(1.0, "Download complete")
            return str(out_path)
        except Exception as exc:
            attempt += 1
            cb(0.0, f"Download attempt {attempt} failed: {exc}")
            tmp_path.unlink(missing_ok=True)
            if attempt >= max_retries:
                raise DriveError(f"Download failed after {max_retries} attempts: {exc}") from exc
            time.sleep(min(30, 2**attempt))


def _stream_download(client, file_id: str, out_path: Path, file_size: int, cb) -> None:
    request = client.files().get_media(fileId=file_id)
    fh = open(out_path, "wb")
    total = 0
    try:
        chunk = request.next_chunk()
        while chunk is not None:
            data = chunk.get_raw_content() if hasattr(chunk, "get_raw_content") else None
            if data is None:
                data = chunk.get() if isinstance(chunk, io.IOBase) else b""
            if data:
                fh.write(data)
                total += len(data)
                if file_size:
                    cb(min(0.99, total / file_size), f"{total / 1048576:.1f} MB")
            chunk = request.next_chunk()
    finally:
        fh.close()
    if file_size and total != file_size:
        raise DriveError(f"Download incomplete: {total} != {file_size} bytes")


def download_url_available(client, file_id: str) -> bool:
    try:
        meta = get_file_meta(client, file_id)
        return bool(meta.get("downloadable"))
    except Exception:
        return False


def oauth_state() -> str:
    return secrets.token_urlsafe(24)
