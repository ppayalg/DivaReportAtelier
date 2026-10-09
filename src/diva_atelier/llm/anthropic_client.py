"""Anthropic Messages API client built on the standard library (no SDK dependency)."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from .base import LLMError, LLMResponse

DEFAULT_MODEL = "claude-sonnet-5-5"
API_VERSION = "2023-06-01"
_RETRYABLE = {408, 409, 429, 500, 502, 503, 504, 529}


class AnthropicClient:
    name = "anthropic"

    def __init__(self, api_key: str | None = None, model: str | None = None,
                 base_url: str = "https://api.anthropic.com", timeout: float = 60.0,
                 max_retries: int = 3, backoff: float = 1.0):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not self.api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set.")
        self.model = model or os.environ.get("DIVA_MODEL", DEFAULT_MODEL)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff

    def complete(self, *, system: str, prompt: str, task: str = "write",
                 max_tokens: int = 1500, temperature: float = 0.3,
                 meta: dict[str, Any] | None = None) -> LLMResponse:
        body = json.dumps({
            "model": self.model, "max_tokens": max_tokens, "temperature": temperature,
            "system": system, "messages": [{"role": "user", "content": prompt}],
        }).encode()
        headers = {"content-type": "application/json", "x-api-key": self.api_key,
                   "anthropic-version": API_VERSION}
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(f"{self.base_url}/v1/messages", data=body,
                                         headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read())
                text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
                usage = data.get("usage", {})
                return LLMResponse(text=text, model=data.get("model", self.model),
                                   input_tokens=usage.get("input_tokens", 0),
                                   output_tokens=usage.get("output_tokens", 0))
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code not in _RETRYABLE or attempt == self.max_retries:
                    detail = exc.read().decode(errors="replace")[:300]
                    raise LLMError(f"Anthropic API returned {exc.code}: {detail}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                last = exc
                if attempt == self.max_retries:
                    raise LLMError(f"Anthropic API unreachable: {exc}") from exc
            time.sleep(self.backoff * (2 ** attempt))
        raise LLMError(f"Anthropic API failed: {last}")
