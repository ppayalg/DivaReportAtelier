"""Provider-neutral LLM interface used by every agent."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class LLMResponse:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


class LLMError(RuntimeError):
    """Raised when a provider call fails after retries."""


class LLMClient(Protocol):
    name: str

    def complete(
        self, *, system: str, prompt: str, task: str = "write",
        max_tokens: int = 1500, temperature: float = 0.3,
        meta: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """Run one completion.

        ``task`` labels the agent step ("write", "qa"). ``meta`` carries the same
        information in structured form; real providers ignore it, the offline
        mock uses it so it never has to parse prompts.
        """
        ...
