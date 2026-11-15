"""Regression tests: atomic OAuth state consumption.

Verifies the callback consumes state atomically with a conditional UPDATE so
two concurrent callbacks cannot both consume the same state, that consumption
is persisted before the code exchange, and that expiry/denial/exchange-failure
are handled.
"""
import threading
from datetime import timedelta

import pytest

from app.db import get_session_factory, utcnow
from app.models import OAuthState, User
from app.services import drive_google


@pytest.fixture()
def oauth_configured(client, app_env, monkeypatch):
    from app.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "google_oauth_client_id", "test-client")
    monkeypatch.setattr(s, "google_oauth_client_secret", "test-secret")
    return app_env


def _start_state(client, headers):
    r = client.get("/api/drive/auth/start", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["auth_url"], r.json()["auth_url"].split("state=")[-1]


def _exchange_ok(monkeypatch):
    monkeypatch.setattr(drive_google, "exchange_code", lambda code: {
        "access_token": "tok", "refresh_token": "ref", "expires_in": 3600,
    })
    monkeypatch.setattr(drive_google, "client_from_connection", lambda conn: (
        object(), {"email": "u@example.com"}
    ))


def test_successful_callback_consumes_state(client, auth_headers, oauth_configured, monkeypatch):
    _exchange_ok(monkeypatch)
    _, state = _start_state(client, auth_headers)
    r = client.get("/api/drive/auth/callback", params={"code": "abc", "state": state})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    session = get_session_factory()()
    try:
        row = session.query(OAuthState).filter(OAuthState.state == state).first()
        assert row.used is True
    finally:
        session.close()


def test_concurrent_reuse_blocked(client, auth_headers, oauth_configured, monkeypatch):
    """Two concurrent callbacks for the same state: only one can consume it."""
    _exchange_ok(monkeypatch)
    _, state = _start_state(client, auth_headers)

    results = []
    barrier = threading.Barrier(2)

    def _hit():
        barrier.wait()
        r = client.get("/api/drive/auth/callback",
                       params={"code": "abc", "state": state})
        results.append(r.status_code)

    threads = [threading.Thread(target=_hit) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    # exactly one success (200), the other must be rejected (400)
    assert results.count(200) == 1, results
    assert results.count(400) == 1, results


def test_expired_state_rejected(client, auth_headers, oauth_configured, monkeypatch):
    _exchange_ok(monkeypatch)
    _, state = _start_state(client, auth_headers)
    # backdate beyond TTL
    session = get_session_factory()()
    try:
        row = session.query(OAuthState).filter(OAuthState.state == state).first()
        row.created_at = utcnow() - timedelta(
            seconds=drive_google.settings.oauth_state_ttl_seconds + 10
        )
        session.commit()
    finally:
        session.close()
    r = client.get("/api/drive/auth/callback", params={"code": "abc", "state": state})
    assert r.status_code == 400
    assert "expired" in r.text


def test_denied_authorization(client, auth_headers, oauth_configured):
    r = client.get("/api/drive/auth/callback",
                   params={"error": "access_denied", "state": "x"})
    assert r.status_code == 400
    assert "denied" in r.text


def test_failed_token_exchange_consumes_state(client, auth_headers, oauth_configured, monkeypatch):
    """A failed code exchange still burns the state (consumed before exchange)."""
    def _boom(code):
        raise drive_google.DriveError("token endpoint rejected")
    monkeypatch.setattr(drive_google, "exchange_code", _boom)
    _, state = _start_state(client, auth_headers)

    r = client.get("/api/drive/auth/callback", params={"code": "bad", "state": state})
    assert r.status_code == 502
    assert "Token exchange failed" in r.text

    # state must be consumed even though the exchange failed
    session = get_session_factory()()
    try:
        row = session.query(OAuthState).filter(OAuthState.state == state).first()
        assert row.used is True
    finally:
        session.close()

    # replay is now rejected
    r2 = client.get("/api/drive/auth/callback", params={"code": "bad", "state": state})
    assert r2.status_code == 400
    assert "already used" in r2.text


def test_unknown_state_rejected(client, auth_headers, oauth_configured):
    r = client.get("/api/drive/auth/callback", params={"code": "abc", "state": "nope"})
    assert r.status_code == 400
    assert "unknown" in r.text or "Invalid" in r.text


def test_missing_params(client, oauth_configured):
    r = client.get("/api/drive/auth/callback")
    assert r.status_code == 400
