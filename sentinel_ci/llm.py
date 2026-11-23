"""Minimal Gemini client wrapper for the LLM backed agents.

SENTINEL CI uses the Google AI Studio Gemini API on its free tier.
This wrapper speaks to that API over plain HTTPS (``requests`` when
available, stdlib ``urllib`` otherwise), so no vendor SDK is required
and Stage 1 keeps its zero dependency footprint. The key is read from
the environment via :class:`~sentinel_ci.config.Config`
(``GEMINI_API_KEY``) and is never logged.

Stage 2 agents call :func:`complete` and get a clear error if the key
is missing, instead of a failure buried at first use.
"""

from __future__ import annotations

import json
import urllib.request

from .config import Config

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"

try:  # pragma: no cover - depends on environment
    import requests

    _HAVE_REQUESTS = True
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]
    _HAVE_REQUESTS = False


class LLMError(RuntimeError):
    pass


def _generate(config: Config, payload: dict) -> dict:
    if not config.gemini_api_key:
        raise LLMError("GEMINI_API_KEY is not set")
    url = f"{API_ROOT}/models/{config.model}:generateContent"
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": config.gemini_api_key,
    }
    if _HAVE_REQUESTS:
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        if resp.status_code >= 400:
            raise LLMError(f"Gemini API -> {resp.status_code}: {resp.text[:200]}")
        return resp.json()
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - fixed host
        return json.loads(resp.read().decode("utf-8"))


def complete(config: Config, system: str, prompt: str, max_tokens: int = 2048) -> str:
    """One shot completion. Stage 2 agents build their prompts on top."""
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    data = _generate(config, payload)
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError) as exc:
        raise LLMError(f"unexpected Gemini response shape: {str(data)[:200]}") from exc
    return "".join(part.get("text", "") for part in parts)
