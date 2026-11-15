"""Regression tests: drives.list() pagination and Shared Drive roots.

Verifies list_roots() uses only Drive API v3 ``drives.list`` parameters, pages
through ``nextPageToken`` completely, always includes My Drive, and raises
DriveError (instead of returning a partial listing) when the API fails.
"""
import pytest

from app import db as dbmod
from app.db import get_session_factory
from app.models import DriveConnection, User
from app.services import drive_google


class _Req:
    def __init__(self, data=None, error=None):
        self._data = data
        self._error = error

    def execute(self):
        if self._error is not None:
            raise self._error
        return self._data


class _Drives:
    """Strict drives.list fake: only supported params, scripted pages."""

    SUPPORTED = {"pageSize", "pageToken", "fields", "q", "orderBy"}

    def __init__(self, pages, fail_on_page=None):
        self.pages = list(pages)
        self.fail_on_page = fail_on_page
        self.calls = []

    def list(self, **params):
        unsupported = set(params) - self.SUPPORTED
        assert not unsupported, f"drives.list() does not support: {unsupported}"
        self.calls.append(params)
        page_index = len(self.calls) - 1
        if self.fail_on_page is not None and page_index == self.fail_on_page:
            return _Req(error=RuntimeError("drives.list exploded"))
        assert page_index < len(self.pages), "unexpected extra page request"
        return _Req(self.pages[page_index])


class _Client:
    def __init__(self, pages, fail_on_page=None):
        self._drives = _Drives(pages, fail_on_page)

    def drives(self):
        return self._drives


def test_single_page_includes_my_drive_and_shared_drives():
    client = _Client([{"drives": [{"id": "sd-1", "name": "Shared One"}]}])
    roots = drive_google.list_roots(client)
    assert roots[0] == {"id": "root", "name": "My Drive", "kind": "folder", "is_root": True}
    assert {"id": "sd-1", "name": "Shared One", "kind": "drive", "is_root": True} in roots
    # only supported params were sent
    assert set(client._drives.calls[0]) <= {"pageSize", "pageToken", "fields"}


def test_pagination_follows_next_page_token_to_the_end():
    pages = [
        {"drives": [{"id": f"sd-{i}", "name": f"Shared {i}"} for i in range(100)],
         "nextPageToken": "page-2"},
        {"drives": [{"id": "sd-100", "name": "Shared 100"},
                    {"id": "sd-101", "name": "Shared 101"}],
         "nextPageToken": "page-3"},
        {"drives": [{"id": "sd-200", "name": "Shared 200"}]},
    ]
    client = _Client(pages)
    roots = drive_google.list_roots(client)
    ids = [r["id"] for r in roots]
    assert ids[0] == "root"
    assert len(ids) == 1 + 103
    assert "sd-100" in ids and "sd-101" in ids and "sd-200" in ids
    # three pages were requested, with the tokens chained correctly
    assert len(client._drives.calls) == 3
    assert "pageToken" not in client._drives.calls[0]
    assert client._drives.calls[1]["pageToken"] == "page-2"
    assert client._drives.calls[2]["pageToken"] == "page-3"


def test_api_failure_on_first_page_raises_drive_error():
    client = _Client([], fail_on_page=0)
    with pytest.raises(drive_google.DriveError):
        drive_google.list_roots(client)


def test_api_failure_on_later_page_raises_drive_error():
    pages = [
        {"drives": [{"id": "sd-1", "name": "Shared One"}], "nextPageToken": "page-2"},
    ]
    client = _Client(pages, fail_on_page=1)
    with pytest.raises(drive_google.DriveError):
        drive_google.list_roots(client)


def test_empty_shared_drive_listing_returns_only_my_drive():
    client = _Client([{"drives": []}])
    roots = drive_google.list_roots(client)
    assert roots == [{"id": "root", "name": "My Drive", "kind": "folder", "is_root": True}]


def _connect_drive(monkeypatch):
    session = get_session_factory()()
    try:
        admin = session.query(User).filter(User.is_admin == True).first()  # noqa: E712
        conn = DriveConnection(
            user_id=admin.id, status="connected", account_email="u@example.com",
            encrypted_access_token="enc", token_expires_at=None,
        )
        session.add(conn)
        session.commit()
        return conn.id
    finally:
        session.close()


def test_drive_roots_endpoint_returns_nodes(client, auth_headers, app_env, monkeypatch):
    monkeypatch.setattr(
        drive_google, "client_from_connection", lambda conn: (object(), {"email": "u@example.com"})
    )
    monkeypatch.setattr(
        drive_google, "list_roots",
        lambda c: [
            {"id": "root", "name": "My Drive", "kind": "folder", "is_root": True},
            {"id": "sd-1", "name": "Shared One", "kind": "drive", "is_root": True},
        ],
    )
    _connect_drive(monkeypatch)
    resp = client.get("/api/drive/roots", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [n["id"] for n in body] == ["root", "sd-1"]


def test_drive_roots_endpoint_does_not_mask_api_failure(client, auth_headers, app_env, monkeypatch):
    monkeypatch.setattr(
        drive_google, "client_from_connection", lambda conn: (object(), {"email": "u@example.com"})
    )

    def _boom(_client):
        raise drive_google.DriveError("Failed to list Shared Drives: boom")

    monkeypatch.setattr(drive_google, "list_roots", _boom)
    _connect_drive(monkeypatch)
    resp = client.get("/api/drive/roots", headers=auth_headers)
    assert resp.status_code == 502
    assert "Failed to list Shared Drives" in resp.json()["detail"]
