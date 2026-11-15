"""Tests for the Google Drive integration with a mocked Drive API."""
from datetime import datetime, timezone

import pytest

from app.services import drive_google


# ---------------------------------------------------------------------------
# Fake Drive API
# ---------------------------------------------------------------------------
class _FakeRequest:
    def __init__(self, data=None, error=None):
        self._data = data
        self._error = error

    def execute(self):
        if self._error:
            raise self._error
        return self._data


class _FakeFiles:
    def __init__(self, tree, page_size=2):
        self.tree = tree  # id -> file dict
        self.page_size = page_size
        self.list_calls = 0

    def list(self, **params):
        self.list_calls += 1
        q = params.get("q", "")
        folder_id = q.split("'")[1]
        children = [f for f in self.tree.values()
                    if folder_id in (f.get("parents") or []) and not f.get("trashed")]
        # emulate pagination
        page_token = params.get("pageToken")
        start = 0
        if page_token:
            start = int(page_token)
        page = children[start:start + self.page_size]
        next_token = str(start + self.page_size) if start + self.page_size < len(children) else None
        return _FakeRequest({"files": page, "nextPageToken": next_token})

    def get(self, fileId, fields=None):
        return _FakeRequest(self.tree[fileId])

    def get_media(self, fileId):
        return _FakeMedia(self.tree[fileId])


class _FakeStatus:
    """Mimics the googleapiclient HttpResponse status used by MediaIoBaseDownload."""

    def __init__(self, fraction: float):
        self._fraction = fraction

    def progress(self) -> float:
        return self._fraction


class _FakeResp:
    """httplib2-style response with a .status attribute and header access."""

    def __init__(self, status: int, headers: dict):
        self.status = status
        self._headers = headers

    def __contains__(self, key):
        return key in self._headers

    def __getitem__(self, key):
        return self._headers[key]

    def get(self, key, default=None):
        return self._headers.get(key, default)


class _FakeHttp:
    """Mimics the httplib2-style http object used by MediaIoBaseDownload."""

    def __init__(self, data: bytes):
        self._data = data

    def request(self, uri, method="GET", body=None, headers=None, **kwargs):
        resp = _FakeResp(
            200,
            {"content-range": f"bytes 0-{len(self._data) - 1}/{len(self._data)}"},
        )
        return resp, self._data


class _FakeMedia:
    """Mimics a googleapiclient media request compatible with MediaIoBaseDownload."""

    def __init__(self, file_meta):
        self.uri = f"https://example.invalid/{file_meta.get('id', '')}"
        self.headers = {}
        self.http = _FakeHttp((file_meta.get("name", "") + "-bytes").encode())


class _FakeChunk:
    def __init__(self, data):
        self._data = data

    def get_raw_content(self):
        return self._data


class _FakeDrives:
    def list(self, **params):
        return _FakeRequest({"drives": [{"id": "shared-1", "name": "Shared Drive"}]})


class _FakeDrive:
    def __init__(self, tree):
        self._files = _FakeFiles(tree)

    def files(self):
        return self._files

    def drives(self):
        return _FakeDrives()

    def about(self):
        return _FakeRequest({"user": {"email": "tester@example.com"}})


def _file(fid, name, mime, parents, **extra):
    f = {"id": fid, "name": name, "mimeType": mime, "parents": parents,
         "modifiedTime": "2026-01-01T00:00:00.000Z"}
    f.update(extra)
    return f


@pytest.fixture()
def drive_tree():
    return {
        "root": {"id": "root", "name": "My Drive", "mimeType": "application/vnd.google-apps.folder",
                 "parents": []},
        "f1": _file("f1", "Podcasts", "application/vnd.google-apps.folder", ["root"]),
        "f2": _file("f2", "Episode 01", "application/vnd.google-apps.folder", ["f1"]),
        "f3": _file("f3", "Episode 02", "application/vnd.google-apps.folder", ["f1"]),
        "v1": _file("v1", "full_episode.mp4", "video/mp4", ["f2"], size="100",
                    capabilities={"canDownload": True}, md5Checksum="abc"),
        "v2": _file("v2", "camera_02.mov", "video/quicktime", ["f2"], size="200",
                    capabilities={"canDownload": True}),
        "v3": _file("v3", "episode.mp4", "video/mp4", ["f3"], size="300",
                    capabilities={"canDownload": True}),
        "v4": _file("v4", "notes.txt", "text/plain", ["f1"], size="10"),
        "link1": _file("link1", "shortcut", "application/vnd.google-apps.shortcut", ["f1"],
                       shortcutDetails={"targetId": "f3"}),
        "link2": _file("link2", "self", "application/vnd.google-apps.shortcut", ["f1"],
                       shortcutDetails={"targetId": "f1"}),  # circular
    }


# ---------------------------------------------------------------------------
def test_is_video_file():
    assert drive_google.is_video_file("video/mp4", "a.mp4")
    assert drive_google.is_video_file("", "a.MKV")
    assert drive_google.is_video_file("application/octet-stream", "a.webm")
    assert not drive_google.is_video_file("text/plain", "a.txt")
    assert not drive_google.is_video_file("application/pdf", "a.pdf")


def test_file_entry_parsing():
    f = {"id": "x", "name": "a.mp4", "mimeType": "video/mp4", "size": "10",
         "modifiedTime": "2026-01-01T00:00:00.000Z", "parents": ["p"],
         "md5Checksum": "m", "version": "3",
         "capabilities": {"canDownload": True}}
    e = drive_google._file_entry(f)
    assert e["modified_at"] is not None
    assert e["downloadable"] is True
    assert e["version"] == "3"
    assert "downloadUrl" not in e


def test_file_entry_workspace_file_not_downloadable():
    # Google Workspace files are not directly downloadable as media.
    f = {"id": "d", "name": "doc", "mimeType": "application/vnd.google-apps.document",
         "size": "0", "capabilities": {"canDownload": True}}
    e = drive_google._file_entry(f)
    assert e["downloadable"] is False


def test_file_entry_downloadable_from_capabilities():
    # downloadable is determined by capabilities.canDownload, not downloadUrl.
    f = {"id": "y", "name": "b.mp4", "mimeType": "video/mp4", "size": "5",
         "capabilities": {"canDownload": False}}
    e = drive_google._file_entry(f)
    assert e["downloadable"] is False


def test_list_children_pagination(drive_tree):
    client = _FakeDrive(drive_tree)
    children = drive_google.list_children(client, "f1")
    names = {c["name"] for c in children}
    assert "notes.txt" in names
    assert "shortcut" in names


def test_list_roots_includes_shared_drives(drive_tree):
    client = _FakeDrive(drive_tree)
    roots = drive_google.list_roots(client)
    ids = {r["id"] for r in roots}
    assert "root" in ids and "shared-1" in ids


def test_recursive_scan_deep_nested(drive_tree):
    client = _FakeDrive(drive_tree)
    progress = []
    videos, stats = drive_google.scan_folders(client, ["root"], progress_cb=lambda p, m: progress.append(m))
    names = {v["name"] for v in videos}
    assert names == {"full_episode.mp4", "camera_02.mov", "episode.mp4"}
    assert stats["videos"] == 3
    # relative paths preserved
    ep = next(v for v in videos if v["name"] == "full_episode.mp4")
    assert ep["relative_path"] == "Podcasts/Episode 01/full_episode.mp4"
    assert progress


def test_recursive_scan_dedup_and_exclude(drive_tree):
    client = _FakeDrive(drive_tree)
    videos, _ = drive_google.scan_folders(client, ["root", "f1"])  # overlapping roots
    ids = [v["id"] for v in videos]
    assert len(ids) == len(set(ids)) == 3
    videos2, _ = drive_google.scan_folders(client, ["root"], exclude_ids=["f2"])
    names = {v["name"] for v in videos2}
    assert "full_episode.mp4" not in names
    assert "episode.mp4" in names  # f3 still reachable via f1


def test_recursive_scan_shortcut_resolution(drive_tree):
    client = _FakeDrive(drive_tree)
    videos, _ = drive_google.scan_folders(client, ["f1"])
    names = {v["name"] for v in videos}
    assert "episode.mp4" in names  # via shortcut link1 -> f3
    assert "self" not in names


def test_download_file_streaming(drive_tree, tmp_path, app_env):
    client = _FakeDrive(drive_tree)
    out = tmp_path / "v1.mp4"
    progress = []
    path = drive_google.download_file(client, "v1", out, file_size=0,
                                      progress_cb=lambda p, m: progress.append(p))
    assert path == str(out)
    assert out.read_bytes() == b"full_episode.mp4-bytes"
    assert progress


def test_download_size_limit(drive_tree, tmp_path, app_env):
    client = _FakeDrive(drive_tree)
    import app.config

    old = app.config.settings.max_download_size
    app.config.settings.max_download_size = 10
    try:
        with pytest.raises(drive_google.DriveError):
            drive_google.download_file(client, "v1", tmp_path / "x.mp4", file_size=1000)
    finally:
        app.config.settings.max_download_size = old


def test_oauth_url_and_state():
    url = drive_google.build_auth_url("state123")
    assert "accounts.google.com" in url
    assert "drive.readonly" in url
    assert "state=state123" in url
    assert len(drive_google.oauth_state()) > 10


def test_token_encryption_flow():
    enc = drive_google.encrypt_secret("tok")
    assert drive_google.decrypt_secret(enc) == "tok"
