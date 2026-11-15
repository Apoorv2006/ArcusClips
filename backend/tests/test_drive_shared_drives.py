"""Regression tests: Shared Drive compatibility.

Verifies supportsAllDrives is passed to metadata and media requests, and that
driveId/corpora="drive" are used when operating within a specific shared drive,
for both My Drive and Shared Drive workflows.
"""
import pytest

from app.db import get_session_factory
from app.models import DriveConnection, DriveFile, User
from app.services import drive_google


class _Req:
    def __init__(self, data=None):
        self._data = data or {}

    def execute(self):
        return self._data


class _FakeResp:
    def __init__(self, status, headers):
        self.status = status
        self._headers = headers

    def __contains__(self, key):
        return key in self._headers

    def __getitem__(self, key):
        return self._headers[key]


class _FakeHttp:
    def __init__(self, data):
        self._data = data

    def request(self, uri, method="GET", body=None, headers=None, **kwargs):
        resp = _FakeResp(200, {"content-range": f"bytes 0-{len(self._data) - 1}/{len(self._data)}"})
        return resp, self._data


class _FakeMedia:
    def __init__(self, meta):
        self.uri = f"https://x/{meta.get('id')}"
        self.headers = {}
        self.http = _FakeHttp(b"x-bytes")


class _RecordingFiles:
    """Records the params passed to list/get/get_media."""

    def __init__(self, tree):
        self.tree = tree
        self.list_params = []
        self.get_params = []
        self.media_params = []

    def list(self, **params):
        # Drive API v3 rules: driveId is only valid with corpora="drive"; all
        # other list params used must belong to the supported set.
        if "driveId" in params:
            assert params.get("corpora") == "drive", (
                "driveId is only valid together with corpora='drive'"
            )
        else:
            assert "corpora" not in params, "corpora='drive' requires driveId"
        unsupported = set(params) - {
            "q", "pageToken", "pageSize", "fields", "orderBy", "spaces",
            "corpora", "driveId", "supportsAllDrives", "includeItemsFromAllDrives",
        }
        assert not unsupported, f"Unsupported files.list params: {unsupported}"
        self.list_params.append(params)
        q = params.get("q", "")
        folder_id = q.split("'")[1]
        children = [f for f in self.tree.values() if folder_id in (f.get("parents") or [])]
        return _Req({"files": children, "nextPageToken": None})

    def get(self, fileId, fields=None, **kwargs):
        assert "driveId" not in kwargs, "files.get() does not support driveId"
        self.get_params.append({"fileId": fileId, **kwargs})
        return _Req(self.tree[fileId])

    def get_media(self, fileId, **kwargs):
        assert "driveId" not in kwargs, "files.get_media() does not support driveId"
        self.media_params.append({"fileId": fileId, **kwargs})
        return _FakeMedia(self.tree[fileId])


class _FakeDrive:
    def __init__(self, tree):
        self._files = _RecordingFiles(tree)

    def files(self):
        return self._files

    def drives(self):
        class _Drives:
            def list(self, **params):
                unsupported = set(params) - {
                    "pageSize", "pageToken", "fields", "q", "orderBy",
                }
                assert not unsupported, (
                    f"drives.list() does not support: {unsupported}"
                )
                return _Req({"drives": [{"id": "shared-1", "name": "Shared Drive"}]})
        return _Drives()

    def about(self):
        return _Req({"user": {"email": "u@example.com"}})


TREE = {
    "root": {"id": "root", "name": "My Drive",
             "mimeType": "application/vnd.google-apps.folder", "parents": []},
    "sd-root": {"id": "sd-root", "name": "Shared Drive",
                "mimeType": "application/vnd.google-apps.folder", "parents": []},
    "v1": {"id": "v1", "name": "a.mp4", "mimeType": "video/mp4", "parents": ["root"],
           "size": "10", "modifiedTime": "2026-01-01T00:00:00.000Z",
           "capabilities": {"canDownload": True}},
    "sv1": {"id": "sv1", "name": "b.mp4", "mimeType": "video/mp4", "parents": ["sd-root"],
            "size": "20", "modifiedTime": "2026-01-01T00:00:00.000Z",
            "capabilities": {"canDownload": True}},
}


@pytest.fixture()
def shared_drive_client(app_env, monkeypatch):
    from app.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "google_oauth_client_id", "c")
    monkeypatch.setattr(s, "google_oauth_client_secret", "x")
    client = _FakeDrive(TREE)
    return client


def test_list_children_my_drive_passes_supports_all_drives(shared_drive_client):
    """My Drive listing still passes supportsAllDrives (harmless, required by API)."""
    drive_google.list_children(shared_drive_client, "root")
    params = shared_drive_client._files.list_params[-1]
    assert params["supportsAllDrives"] is True
    assert params["includeItemsFromAllDrives"] is True
    assert "driveId" not in params  # My Drive: no driveId
    assert "corpora" not in params


def test_list_children_shared_drive_uses_drive_id_and_corpora(shared_drive_client):
    """Shared Drive listing scopes with driveId + corpora=drive."""
    drive_google.list_children(shared_drive_client, "sd-root", drive_id="shared-1")
    params = shared_drive_client._files.list_params[-1]
    assert params["supportsAllDrives"] is True
    assert params["includeItemsFromAllDrives"] is True
    assert params["driveId"] == "shared-1"
    assert params["corpora"] == "drive"


def test_get_file_meta_always_passes_supports_all_drives(shared_drive_client):
    """get_file_meta always passes supportsAllDrives and never driveId."""
    drive_google.get_file_meta(shared_drive_client, "v1")
    p = shared_drive_client._files.get_params[-1]
    assert p["supportsAllDrives"] is True
    assert "driveId" not in p

    # files.get() must not carry driveId even for Shared Drive files
    drive_google.get_file_meta(shared_drive_client, "sv1")
    p2 = shared_drive_client._files.get_params[-1]
    assert p2["supportsAllDrives"] is True
    assert "driveId" not in p2
    assert "corpora" not in p2


def test_download_passes_supports_all_drives_never_drive_id(shared_drive_client, tmp_path):
    """Media download requests carry supportsAllDrives and never driveId."""
    out = tmp_path / "a.mp4"
    drive_google.download_file(shared_drive_client, "v1", out, file_size=0)
    p = shared_drive_client._files.media_params[-1]
    assert p["supportsAllDrives"] is True
    assert "driveId" not in p  # My Drive download

    # files.get_media() must not carry driveId even for Shared Drive targets
    out2 = tmp_path / "b.mp4"
    drive_google.download_file(shared_drive_client, "sv1", out2, file_size=0)
    p2 = shared_drive_client._files.media_params[-1]
    assert p2["supportsAllDrives"] is True
    assert "driveId" not in p2
    assert "corpora" not in p2


def test_scan_shared_drive_propagates_drive_id(shared_drive_client):
    """scan_folders propagates drive_id to listing and records it on videos."""
    videos, _ = drive_google.scan_folders(shared_drive_client, ["sd-root"],
                                          drive_id="shared-1")
    assert len(videos) == 1
    assert videos[0]["id"] == "sv1"
    assert videos[0]["drive_id"] == "shared-1"
    # the listing call was scoped to the shared drive
    params = shared_drive_client._files.list_params[-1]
    assert params["driveId"] == "shared-1"
    assert params["corpora"] == "drive"


def test_scan_my_drive_videos_have_no_drive_id(shared_drive_client):
    """My Drive scanned videos carry an empty/absent drive_id."""
    videos, _ = drive_google.scan_folders(shared_drive_client, ["root"])
    assert len(videos) == 1
    assert videos[0]["drive_id"] is None


def test_list_roots_includes_shared_drives(shared_drive_client):
    roots = drive_google.list_roots(shared_drive_client)
    ids = {r["id"] for r in roots}
    assert "root" in ids
    assert "shared-1" in ids
    shared = next(r for r in roots if r["id"] == "shared-1")
    assert shared["kind"] == "drive"
