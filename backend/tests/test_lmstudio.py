"""Tests for the LM Studio client against a mock local OpenAI-compatible server."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.config import get_settings
from app.services import lmstudio


class _MockLMStudio(BaseHTTPRequestHandler):
    loaded_models = ["local-llama-3.1-8b"]
    last_payload = None

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/v1/models":
            self._json(200, {"object": "list",
                             "data": [{"id": m, "object": "model"} for m in self.loaded_models]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        _MockLMStudio.last_payload = payload
        if self.path == "/v1/chat/completions":
            content = json.dumps({"candidates": [{"start": 1.0, "end": 12.0}]})
            self._json(200, {"choices": [{"message": {"role": "assistant", "content": content}}]})
        else:
            self._json(404, {"error": "not found"})

    def log_message(self, *args):
        pass


@pytest.fixture()
def mock_lmstudio(app_env):
    server = HTTPServer(("127.0.0.1", 0), _MockLMStudio)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    s = get_settings()
    s.lmstudio_base_url = f"http://127.0.0.1:{port}/v1"
    yield s
    server.shutdown()
    thread.join(timeout=2)


def test_test_connection_success(mock_lmstudio):
    result = lmstudio.test_connection()
    assert result["connected"]
    assert "local-llama-3.1-8b" in result["models"]


def test_list_and_resolve_model(mock_lmstudio):
    assert lmstudio.resolve_model() == "local-llama-3.1-8b"
    assert lmstudio.resolve_model("other-model") == "other-model"


def test_chat_and_json(mock_lmstudio):
    content = lmstudio.chat([{"role": "user", "content": "hi"}], model="local-llama-3.1-8b")
    data = json.loads(content)
    assert data["candidates"][0]["end"] == 12.0
    parsed = lmstudio.chat_json([{"role": "user", "content": "give json"}])
    assert parsed["candidates"][0]["start"] == 1.0


def test_extract_json_variants():
    assert lmstudio.extract_json('{"a": 1}') == {"a": 1}
    assert lmstudio.extract_json('```json\n{"a": 2}\n```') == {"a": 2}
    assert lmstudio.extract_json('prefix {"a": 3} suffix') == {"a": 3}
    with pytest.raises(lmstudio.LMStudioError):
        lmstudio.extract_json("no json at all")


def test_connection_failure(app_env, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "lmstudio_base_url", "http://127.0.0.1:9/v1", raising=False)
    result = lmstudio.test_connection()
    assert not result["connected"]
    assert result["error"]
    with pytest.raises(lmstudio.LMStudioError):
        lmstudio.chat([{"role": "user", "content": "hi"}])


class _MockRejectResponseFormat(BaseHTTPRequestHandler):
    """LM Studio builds that reject 'json_object' (HTTP 400) but accept
    'json_schema' or plain text."""
    loaded_models = ["local-llama-3.1-8b"]
    reject = {"json_object"}
    posts: list = []

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/v1/models":
            self._json(200, {"object": "list",
                             "data": [{"id": m, "object": "model"} for m in self.loaded_models]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        type(self).posts.append(payload)
        rf = (payload.get("response_format") or {}).get("type", "text")
        if rf in type(self).reject:
            self._json(400, {"error": "'response_format.type' must be 'json_schema' or 'text'"})
            return
        content = json.dumps({"candidates": [{"start": 1.0, "end": 12.0}]})
        if rf == "text":
            content = f"Sure! Here you go:\n{content}"
        self._json(200, {"choices": [{"message": {"role": "assistant", "content": content}}]})

    def log_message(self, *args):
        pass


@pytest.fixture()
def mock_rejecting_lmstudio(app_env, monkeypatch):
    _MockRejectResponseFormat.reject = {"json_object"}
    _MockRejectResponseFormat.posts = []
    monkeypatch.setattr(lmstudio, "_json_rf_mode", "json_object")
    server = HTTPServer(("127.0.0.1", 0), _MockRejectResponseFormat)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    s = get_settings()
    s.lmstudio_base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    yield s
    server.shutdown()
    thread.join(timeout=2)


def test_chat_json_adapts_json_object_to_json_schema(mock_rejecting_lmstudio):
    result = lmstudio.chat_json([{"role": "user", "content": "pick moments"}])
    assert result["candidates"][0]["start"] == 1.0
    modes = [(p.get("response_format") or {}).get("type") for p in _MockRejectResponseFormat.posts]
    assert modes == ["json_object", "json_schema"]  # adapted after the 400
    assert lmstudio._json_rf_mode == "json_schema"  # remembered for this process


def test_chat_json_falls_back_to_text_when_schema_also_rejected(mock_rejecting_lmstudio, monkeypatch):
    _MockRejectResponseFormat.reject = {"json_object", "json_schema"}
    result = lmstudio.chat_json([{"role": "user", "content": "pick moments"}])
    assert result["candidates"][0]["end"] == 12.0
    posts = _MockRejectResponseFormat.posts
    modes = [(p.get("response_format") or {}).get("type", "text") for p in posts]
    assert modes == ["json_object", "json_schema", "text"]
    assert "response_format" not in posts[-1]  # text mode sends no response_format
    assert lmstudio._json_rf_mode == "text"
