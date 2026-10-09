"""Shared test helpers."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from diva_atelier.llm.base import LLMResponse

ROOT = Path(__file__).resolve().parents[1]


def load_config(key: str) -> dict[str, Any]:
    return copy.deepcopy(json.loads((ROOT / "configs" / f"{key}.json").read_text(encoding="utf-8")))


def load_vars(key: str) -> dict[str, Any]:
    return json.loads((ROOT / "examples" / f"{key}_vars.json").read_text(encoding="utf-8"))


def load_feedback(key: str = "bridal") -> list[dict[str, Any]]:
    return json.loads((ROOT / "data" / "feedback" / f"{key}.json").read_text(encoding="utf-8"))


class ScriptedLLM:
    """Returns queued responses in order (repeating the last), recording every call."""
    name = "scripted"

    def __init__(self, *texts: str, error: Exception | None = None):
        self.texts, self.calls, self.error = list(texts), [], error

    def complete(self, *, system, prompt, task="write", max_tokens=1500, temperature=0.3, meta=None):
        self.calls.append({"system": system, "prompt": prompt, "task": task, "meta": meta})
        if self.error:
            raise self.error
        text = self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]
        return LLMResponse(text=text, model="scripted", input_tokens=10, output_tokens=5)
