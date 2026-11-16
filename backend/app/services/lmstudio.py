"""LM Studio client — OpenAI-compatible Chat Completions against a local server.

All requests go to the user's own LM Studio instance (default
http://127.0.0.1:1234/v1). No cloud LLM is ever contacted. Structured JSON
output is requested where supported and always validated.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from ..config import settings

logger = logging.getLogger("arcusclips.lmstudio")


class LMStudioError(RuntimeError):
    """Connection or protocol error with the local LM Studio server."""


def _base_url() -> str:
    url = settings.lmstudio_base_url.strip().rstrip("/")
    if not url.endswith("/v1"):
        url = url + "/v1"
    return url


def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if settings.lmstudio_api_key:
        h["Authorization"] = f"Bearer {settings.lmstudio_api_key}"
    return h


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(settings.lmstudio_timeout)


# ---------------------------------------------------------------------------
def test_connection() -> dict[str, Any]:
    """Return {connected, models, error, loaded_model}."""
    url = f"{_base_url()}/models"
    try:
        resp = httpx.get(url, headers=_headers(), timeout=_timeout())
        if resp.status_code != 200:
            return {
                "connected": False,
                "models": [],
                "error": f"LM Studio returned HTTP {resp.status_code}",
                "loaded_model": "",
            }
        data = resp.json()
        models = [m.get("id", "") for m in data.get("data", []) if m.get("id")]
        return {"connected": True, "models": models, "error": "", "loaded_model": ""}
    except httpx.HTTPError as exc:
        return {
            "connected": False,
            "models": [],
            "error": f"Could not connect to LM Studio at {_base_url()}: {exc}",
            "loaded_model": "",
        }


def list_models() -> list[str]:
    res = test_connection()
    return res["models"]


def resolve_model(requested: str | None = None) -> str:
    """Pick the model to use: explicit request > settings > first loaded model."""
    if requested:
        return requested
    if settings.lmstudio_model:
        return settings.lmstudio_model
    models = list_models()
    if not models:
        raise LMStudioError("LM Studio is running but no models are loaded")
    return models[0]


def _json_response_format(mode: str) -> dict | None:
    """response_format payload for a given mode, or None for plain text."""
    if mode == "json_object":
        return {"type": "json_object"}
    if mode == "json_schema":
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "arcus_structured_response",
                "schema": {"type": "object"},
            },
        }
    return None  # text mode: no response_format; extract_json parses the reply


# Which response_format this server accepts. Some LM Studio builds only accept
# 'json_schema' or 'text' (HTTP 400: "'response_format.type' must be
# 'json_schema' or 'text'"); others accept the OpenAI-style 'json_object'.
# We start optimistic and adapt once, then remember the working mode.
_json_rf_mode = "json_object"


def chat(
    messages: list[dict[str, str]],
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    json_mode: bool = False,
) -> str:
    """Send a chat completion request. Returns assistant message content."""
    global _json_rf_mode
    url = f"{_base_url()}/chat/completions"
    payload: dict[str, Any] = {
        "model": model or resolve_model(),
        "messages": messages,
        "temperature": temperature if temperature is not None else settings.lmstudio_temperature,
        "max_tokens": max_tokens or settings.lmstudio_max_tokens,
    }
    if json_mode:
        order = ("json_object", "json_schema", "text")
        stages = list(order[order.index(_json_rf_mode):])
    else:
        stages = [None]

    resp = None
    for mode in stages:
        if mode:
            rf = _json_response_format(mode)
            if rf is not None:
                payload["response_format"] = rf
            else:
                payload.pop("response_format", None)
        try:
            resp = httpx.post(url, json=payload, headers=_headers(), timeout=_timeout())
        except httpx.HTTPError as exc:
            raise LMStudioError(f"Could not reach LM Studio at {_base_url()}: {exc}") from exc
        if (
            resp.status_code == 400
            and json_mode
            and mode != "text"
            and "response_format" in resp.text
        ):
            next_mode = "json_schema" if mode == "json_object" else "text"
            logger.warning(
                "LM Studio rejected response_format %r (%s); adapting to %r",
                mode, resp.text[:200], next_mode,
            )
            _json_rf_mode = next_mode
            continue
        break
    if resp is None or resp.status_code != 200:
        assert resp is not None
        raise LMStudioError(f"LM Studio chat request failed (HTTP {resp.status_code}): {resp.text[:200]}")
    try:
        data = resp.json()
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, ValueError) as exc:
        raise LMStudioError(f"Unexpected LM Studio response shape: {resp.text[:200]}") from exc


def chat_json(
    messages: list[dict[str, str]],
    model: str | None = None,
    temperature: float | None = None,
) -> Any:
    """Chat request that must return a JSON object; content is validated."""
    content = chat(messages, model=model, temperature=temperature, json_mode=True)
    return extract_json(content)


# ---------------------------------------------------------------------------
def extract_json(text: str) -> Any:
    """Parse JSON from an LLM response, tolerating code fences / prose."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Find the outermost JSON object/array
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise LMStudioError(f"LLM response did not contain valid JSON: {text[:200]}")


def is_available() -> bool:
    return test_connection()["connected"]


def health() -> dict[str, Any]:
    res = test_connection()
    res["loaded_model"] = resolve_model() if res["connected"] else ""
    return res
