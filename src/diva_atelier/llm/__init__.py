"""LLM providers. Select one with ``DIVA_LLM_PROVIDER`` (mock | anthropic)."""
from __future__ import annotations

import os

from .base import LLMClient, LLMError, LLMResponse
from .mock import MockLLM


def get_llm(provider: str | None = None) -> LLMClient:
    provider = (provider or os.environ.get("DIVA_LLM_PROVIDER", "mock")).lower()
    if provider == "mock":
        return MockLLM()
    if provider == "anthropic":
        from .anthropic_client import AnthropicClient
        return AnthropicClient()
    raise LLMError(f"Unknown LLM provider '{provider}'. Use 'mock' or 'anthropic'.")


__all__ = ["LLMClient", "LLMError", "LLMResponse", "MockLLM", "get_llm"]
