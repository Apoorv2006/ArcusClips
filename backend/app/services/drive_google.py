"""Google Drive integration — Google Drive API v3 + OAuth 2.0.

Read-only access by default (``drive.readonly`` scope). Tokens are stored
encrypted at rest. The recursive crawler handles pagination, shared drives,
shortcuts, dedup, permission failures and interrupted scans. Downloads stream
to disk and never load full files into memory.
"""
from __future__ import annotations

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

# Valid Drive v3 metadata fields (downloadUrl is NOT valid and is omitted).
_FILE_FIELDS = (
    "nextPageToken, files(id,name,mimeType,size,modifiedTime,parents,"
    "md5Checksum,version,shortcutDetails,capabilities(canDownload))"
)
_FILE_FIELDS_SINGLE = (
    "id,name,mimeType,size,modifiedTime,parents,"
    "md5Checksum,version,shortcutDetails,capabilities(canDownload)"
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


def _looks_like_auth_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in ("401", "invalid_token", "expired", "invalid_grant", "unauthorized")
    )


def client_from_connection(conn) -> Any:
    """Build an authorized Drive client from a stored (encrypted) connection row.

    Refreshes the access token on expiry. If the refresh token is revoked or
    invalid, marks the connection as expired so the user must reauthorize.
    """
    access = decrypt_secret(conn.encrypted_access_token or "")
    refresh = decrypt_secret(conn.encrypted_refresh_token or "") or None
    if not access:
        raise DriveError("No stored Google Drive credentials")
    client = _authorized_client(access, refresh)
    try:
        about = client.about().get(fields="user").execute()
        return client, about.get("user", {})
    except Exception as exc:
        if _looks_like_auth_error(exc):
            if not refresh:
                conn.status = "expired"
                raise DriveError(
                    "Google Drive credentials expired and no refresh token is available; "
                    "reconnect Google Drive"
                ) from exc
            try:
                new = refresh_tokens(refresh)
            except DriveError as refresh_exc:
                # refresh_tokens raises for revoked/invalid refresh tokens
                conn.status = "expired"
                raise DriveError(
                    "Google Drive refresh token is invalid or revoked; reconnect Google Drive"
                ) from refresh_exc
            access = new.get("access_token", access)
            # store_tokens preserves the existing refresh token if none is returned
            store_tokens(conn, new)
            client = _authorized_client(access, decrypt_secret(conn.encrypted_refresh_token or "") or None)
            try:
                about = client.about().get(fields="user").execute()
            except Exception as exc2:
                conn.status = "expired"
                raise DriveError(f"Drive API error after refresh: {exc2}") from exc2
            conn.status = "connected"
            return client, about.get("user", {})
        raise DriveError(f"Drive API error: {exc}") from exc


def store_tokens(conn, tokens: dict[str, Any]) -> None:
    conn.encrypted_access_token = encrypt_secret(tokens.get("access_token", ""))
    # Preserve an existing refresh token when Google does not return a
    # replacement (refresh tokens are long-lived and not always reissued).
    new_refresh = tokens.get("refresh_token")
    if new_refresh:
        conn.encrypted_refresh_token = encrypt_secret(new_refresh)
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
    mime = f.get("mimeType", "")
    # Download availability comes from file capabilities, not a downloadUrl field
    # (downloadUrl is not a valid files.list/files.get metadata field).
    capabilities = f.get("capabilities") or {}
    can_download = bool(capabilities.get("canDownload", False))
    # Google Workspace files (docs/sheets/etc.) are not directly downloadable as
    # media; treat them as non-downloadable unless capabilities say otherwise.
    is_workspace_file = mime.startswith("application/vnd.google-apps.") and mime != (
        "application/vnd.google-apps.shortcut"
    )
    downloadable = can_download and not is_workspace_file
    # For shortcuts, the media lives at the target ID; normal files use their
    # own ID. Both are preserved so downloads use the real file ID.
    target_id = f["id"]
    if is_shortcut and targets:
        target_id = targets[0]
    return {
        "id": f["id"],
        "target_id": target_id,
        "name": f.get("name", ""),
        "mime_type": mime,
        "size": int(f.get("size", 0) or 0),
        "modified_at": _fmt_modified(f.get("modifiedTime")),
        "parent_id": (f.get("parents") or [""])[0],
        "is_shortcut": is_shortcut,
        "downloadable": downloadable,
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
        drives = client.drives().list(
            pageSize=100, supportsAllDrives=True, includeItemsFromAllDrives=True
        ).execute()
        for d in drives.get("drives", []):
            roots.append({"id": d["id"], "name": d.get("name", ""), "kind": "drive", "is_root": True})
    except Exception as exc:
        logger.warning("Shared drives listing failed: %s", exc)
    return roots


def list_children(client, folder_id: str, drive_id: str | None = None) -> list[dict[str, Any]]:
    """List children of a folder (or root) with pagination.

    Supports both My Drive and Shared Drives via ``supportsAllDrives`` and
    ``includeItemsFromAllDrives``. When ``drive_id`` is supplied the query is
    scoped to that shared drive.
    """
    items: list[dict[str, Any]] = []
    page_token = ""
    while True:
        params: dict[str, Any] = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "pageSize": 1000,
            "fields": _FILE_FIELDS,
            "orderBy": "folder,name",
            # Required for Shared Drive operations; harmless for My Drive.
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
        }
        if drive_id:
            params["driveId"] = drive_id
            params["corpora"] = "drive"
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


def get_file_meta(client, file_id: str, drive_id: str | None = None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "fields": _FILE_FIELDS_SINGLE,
        # Required for Shared Drive files; harmless for My Drive.
        "supportsAllDrives": True,
    }
    if drive_id:
        kwargs["driveId"] = drive_id
    f = client.files().get(fileId=file_id, **kwargs).execute()
    return _file_entry(f)


# ---------------------------------------------------------------------------
# Recursive scan
# ---------------------------------------------------------------------------
def _resolve_shortcut_target(
    client, shortcut: dict[str, Any], drive_id: str | None,
    errors: dict[str, str], stats: dict[str, int],
) -> dict[str, Any] | None:
    """Resolve a shortcut's target metadata.

    Returns a dict describing the target with ``is_folder`` / ``is_video``
    flags, or ``None`` when the target is unsupported or inaccessible (an
    error is recorded in ``errors``).
    """
    targets = shortcut.get("shortcut_targets", [])
    if not targets:
        stats["skipped"] += 1
        return None
    target_id = targets[0]
    try:
        meta = get_file_meta(client, target_id, drive_id=drive_id)
    except Exception as exc:
        errors[target_id] = f"Shortcut target inaccessible: {exc}"
        stats["errors"] += 1
        return None
    mime = meta.get("mime_type", "")
    is_folder = mime == "application/vnd.google-apps.folder"
    is_video = is_video_file(mime, meta.get("name", ""))
    if not is_folder and not is_video:
        stats["skipped"] += 1
        return None
    result = dict(meta)
    result["is_folder"] = is_folder
    result["is_video"] = is_video
    return result


def scan_folders(
    client,
    folder_ids: list[str],
    exclude_ids: list[str] | None = None,
    progress_cb: ProgressCb | None = None,
    max_files: int = 10000,
    drive_id: str | None = None,
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
            children = list_children(client, folder_id, drive_id=drive_id)
        except DriveError as exc:
            stats["errors"] += 1
            errors[folder_id] = str(exc)
            logger.warning("Folder %s scan error: %s", folder_id, exc)
            continue

        for child in children:
            stats["files"] += 1
            cid = child["id"]
            if child["is_shortcut"]:
                # A shortcut may point to a folder, a file, or be unsupported.
                # Resolve the target metadata and act accordingly.
                resolved = _resolve_shortcut_target(client, child, drive_id, errors, stats)
                if resolved is None:
                    # unsupported / inaccessible shortcut already recorded
                    continue
                if resolved["is_video"]:
                    if cid not in videos:
                        video = dict(resolved)
                        # Register under the shortcut ID for dedup/display, but
                        # keep the real target ID for media downloads.
                        video["id"] = cid
                        video["target_id"] = resolved["id"]
                        video["name"] = child["name"] or resolved["name"]
                        video["is_shortcut"] = True
                        video["drive_id"] = drive_id
                        video["relative_path"] = "/".join(
                            p for p in ([rel_path, child["name"]] if rel_path else [child["name"]]) if p
                        )
                        videos[cid] = video
                elif resolved["is_folder"]:
                    target_id = resolved["id"]
                    if target_id not in visited and target_id not in exclude:
                        queue.append((target_id, rel_path))
                stats["skipped"] += 1
                continue
            if is_video_file(child["mime_type"], child["name"]):
                if cid not in videos:
                    video = dict(child)
                    # target_id defaults to the file's own id (already in child).
                    video.setdefault("target_id", cid)
                    video["drive_id"] = drive_id
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
    checksum: str = "",
    progress_cb: ProgressCb | None = None,
    max_retries: int = 3,
    cancel_event=None,
    drive_id: str | None = None,
) -> str:
    """Stream a file from Drive to disk using MediaIoBaseDownload.

    Writes to a temporary ``.part`` file, retries transient failures with
    bounded exponential backoff, validates size (and MD5 when supplied) and
    only moves the file to its final destination after successful validation.

    ``drive_id`` scopes the request to a Shared Drive (passes ``supportsAllDrives``
    and ``driveId`` to the media request). The download size limit is enforced
    while streaming even when the provider does not report the expected size.
    """
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
            _media_download(client, file_id, tmp_path, file_size, checksum, cb,
                            cancel_event, drive_id=drive_id)
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


def _media_download(client, file_id: str, out_path: Path, file_size: int,
                    checksum: str, cb, cancel_event, drive_id: str | None = None) -> None:
    """Stream media to ``out_path`` via MediaIoBaseDownload with validation.

    Enforces ``settings.max_download_size`` during streaming (even when
    ``file_size`` is unknown/0) and stops immediately on cancellation or when
    the size limit is exceeded.
    """
    from googleapiclient.http import MediaIoBaseDownload

    kwargs: dict[str, Any] = {"fileId": file_id, "supportsAllDrives": True}
    if drive_id:
        kwargs["driveId"] = drive_id
    request = client.files().get_media(**kwargs)
    fh = open(out_path, "wb")
    written = 0
    max_size = settings.max_download_size
    try:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            if cancel_event is not None and cancel_event.is_set():
                raise DriveError("Download cancelled")
            try:
                status, done = downloader.next_chunk(num_retries=3)
            except Exception:
                raise
            if status:
                # MediaIoBaseDownload writes to the file handle; track bytes.
                written = fh.tell()
                if max_size and written > max_size:
                    raise DriveError(
                        f"Download exceeded max size ({written} > {max_size})"
                    )
                frac = status.progress()
                cb(min(0.99, frac), f"{frac * 100:.0f}%")
    finally:
        fh.close()

    actual_size = out_path.stat().st_size
    if file_size and actual_size != file_size:
        raise DriveError(f"Download size mismatch: got {actual_size}, expected {file_size}")
    if max_size and actual_size > max_size:
        raise DriveError(f"Download exceeded max size ({actual_size} > {max_size})")
    if checksum:
        actual_md5 = _md5_file(out_path)
        if actual_md5.lower() != checksum.lower():
            raise DriveError(
                f"Checksum mismatch: got {actual_md5}, expected {checksum}"
            )


def _md5_file(path: Path) -> str:
    import hashlib

    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_url_available(client, file_id: str) -> bool:
    try:
        meta = get_file_meta(client, file_id)
        return bool(meta.get("downloadable"))
    except Exception:
        return False


def oauth_state() -> str:
    return secrets.token_urlsafe(24)
