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
