"""Regression tests for the Google Drive OAuth callback flow.

Verifies that the callback works without a bearer token, resolves the
initiating user from the OAuth state record, enforces one-time use and expiry,
and handles Google's denial/error responses. Google API calls are mocked.
"""
from datetime import timedelta

import pytest

from app.db import get_session_factory, utcnow
from app.models import OAuthState
from app.services import drive_google
from app.config import get_settings


@pytest.fixture()
def oauth_configured(app_env, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "google_oauth_client_id", "test-client")
    monkeypatch.setattr(s, "google_oauth_client_secret", "test-secret")
    return app_env


def _make_state(client, headers, state=None):
    r = client.get("/api/drive/auth/start", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["auth_url"], (state or r.json().get("auth_url", "").split("state=")[-1])


def test_callback_without_bearer_token(client, auth_headers, oauth_configured, monkeypatch):
    """The callback must not require an Authorization header."""
    monkeypatch.setattr(drive_google, "exchange_code", lambda code: {
        "access_token": "tok", "refresh_token": "ref", "expires_in": 3600,
    })
    monkeypatch.setattr(drive_google, "client_from_connection", lambda conn: (
        object(), {"email": "user@example.com"}
    ))

    auth_url, state = _make_state(client, auth_headers)
    assert "api/drive/auth/callback" in drive_google.settings.google_oauth_redirect_uri

    # no Authorization header on the callback
    r = client.get("/api/drive/auth/callback", params={"code": "abc", "state": state})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert r.json()["account_email"] == "user@example.com"


def test_callback_rejects_unknown_state(client, auth_headers, oauth_configured):
    r = client.get("/api/drive/auth/callback", params={"code": "abc", "state": "bogus"})
    assert r.status_code == 400
    assert "Invalid or unknown OAuth state" in r.text


def test_callback_rejects_replay(client, auth_headers, oauth_configured, monkeypatch):
    monkeypatch.setattr(drive_google, "exchange_code", lambda code: {
        "access_token": "tok", "refresh_token": "ref", "expires_in": 3600,
    })
    monkeypatch.setattr(drive_google, "client_from_connection", lambda conn: (
        object(), {"email": "user@example.com"}
    ))
    auth_url, state = _make_state(client, auth_headers)

    r1 = client.get("/api/drive/auth/callback", params={"code": "abc", "state": state})
    assert r1.status_code == 200
    # second use of the same state must be rejected
    r2 = client.get("/api/drive/auth/callback", params={"code": "abc", "state": state})
    assert r2.status_code == 400
    assert "already used" in r2.text


def test_callback_rejects_expired_state(client, auth_headers, oauth_configured, monkeypatch):
    monkeypatch.setattr(drive_google, "exchange_code", lambda code: {
        "access_token": "tok", "refresh_token": "ref", "expires_in": 3600,
    })
    monkeypatch.setattr(drive_google, "client_from_connection", lambda conn: (
        object(), {"email": "user@example.com"}
    ))
    auth_url, state = _make_state(client, auth_headers)

    # backdate the state beyond the TTL
    session = get_session_factory()()
    try:
        row = session.query(OAuthState).filter(OAuthState.state == state).first()
        row.created_at = utcnow() - timedelta(seconds=drive_google.settings.oauth_state_ttl_seconds + 10)
        session.commit()
    finally:
        session.close()

    r = client.get("/api/drive/auth/callback", params={"code": "abc", "state": state})
    assert r.status_code == 400
    assert "expired" in r.text


def test_callback_handles_google_denial(client, auth_headers, oauth_configured):
    r = client.get("/api/drive/auth/callback", params={"error": "access_denied", "state": "x"})
    assert r.status_code == 400
    assert "denied" in r.text


def test_callback_missing_params(client, oauth_configured):
    r = client.get("/api/drive/auth/callback")
    assert r.status_code == 400


def test_state_resolves_user_not_query_param(client, auth_headers, oauth_configured, monkeypatch):
    """The user is resolved from the state record, not an untrusted param."""
    captured = {}

    def _fake_exchange(code):
        captured["code"] = code
        return {"access_token": "tok", "refresh_token": "ref", "expires_in": 3600}

    monkeypatch.setattr(drive_google, "exchange_code", _fake_exchange)
    monkeypatch.setattr(drive_google, "client_from_connection", lambda conn: (
        object(), {"email": "resolved@example.com"}
    ))
    auth_url, state = _make_state(client, auth_headers)

    # passing a bogus user_id param must be ignored
    r = client.get("/api/drive/auth/callback",
                   params={"code": "the-code", "state": state, "user_id": "99999"})
    assert r.status_code == 200
    assert captured["code"] == "the-code"
    assert r.json()["account_email"] == "resolved@example.com"


def test_tokens_encrypted_at_rest(client, auth_headers, oauth_configured, monkeypatch):
    monkeypatch.setattr(drive_google, "exchange_code", lambda code: {
        "access_token": "super-secret-access", "refresh_token": "super-secret-refresh",
        "expires_in": 3600,
    })
    monkeypatch.setattr(drive_google, "client_from_connection", lambda conn: (
        object(), {"email": "user@example.com"}
    ))
    auth_url, state = _make_state(client, auth_headers)
    r = client.get("/api/drive/auth/callback", params={"code": "abc", "state": state})
    assert r.status_code == 200

    session = get_session_factory()()
    try:
        from app.models import DriveConnection

        conn = session.query(DriveConnection).first()
        assert conn is not None
        # raw tokens must never appear in the stored (encrypted) columns
        assert "super-secret-access" not in (conn.encrypted_access_token or "")
        assert "super-secret-refresh" not in (conn.encrypted_refresh_token or "")
        assert conn.encrypted_access_token  # encrypted value present
    finally:
        session.close()


def test_refresh_token_preserved_when_not_returned():
    """store_tokens preserves an existing refresh token when Google omits one."""

    class _Conn:
        encrypted_access_token = ""
        encrypted_refresh_token = drive_google.encrypt_secret("existing-refresh")
        token_expires_at = None

    conn = _Conn()
    drive_google.store_tokens(conn, {"access_token": "new-access", "expires_in": 3600})
    assert drive_google.decrypt_secret(conn.encrypted_refresh_token) == "existing-refresh"
    assert drive_google.decrypt_secret(conn.encrypted_access_token) == "new-access"


def test_auth_url_uses_canonical_callback(oauth_configured):
    from urllib.parse import parse_qs, urlparse

    url = drive_google.build_auth_url("st")
    qs = parse_qs(urlparse(url).query)
    redirect = qs["redirect_uri"][0]
    assert "api/drive/auth/callback" in redirect
    assert "api/auth/google/callback" not in redirect
