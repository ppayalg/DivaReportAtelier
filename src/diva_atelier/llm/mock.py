"""Deterministic offline model so the whole pipeline runs (and is testable) without an API key.

It is a stand-in, not a language model: it composes text from the instructions and the
retrieved context. Output is stable for identical input, which is what tests need.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .base import LLMResponse


def _first_sentence(text: str, limit: int = 220) -> str:
    text = " ".join(text.split())
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    out = m.group(1) if m else text
    return out[:limit].rstrip()


class MockLLM:
    name = "mock"

    def complete(self, *, system: str, prompt: str, task: str = "write",
                 max_tokens: int = 1500, temperature: float = 0.3,
                 meta: dict[str, Any] | None = None) -> LLMResponse:
        meta = meta or {}
        if task == "qa":
            text = json.dumps({
                "scores": {d: 0.92 for d in ("accuracy", "completeness", "structure", "clarity", "compliance")},
                "checklist": [True] * len(meta.get("checklist", [])),
                "issues": [],
            })
        else:
            text = self._write(meta)
        return LLMResponse(text=text, model="mock-1", input_tokens=len(prompt) // 4,
                           output_tokens=len(text) // 4)

    @staticmethod
    def _write(meta: dict[str, Any]) -> str:
        title = meta.get("title", "Section")
        instructions = meta.get("instructions", "").strip()
        snippets: list[str] = meta.get("snippets", [])
        shape = meta.get("shape", "Narrative")
        limit = meta.get("max_paragraphs")

        paras = [instructions or f"This section covers {title.lower()}."]
        for s in snippets:
            fs = _first_sentence(s) or "no detail recorded."
            paras.append(f"Prior reporting notes: {fs}")
        if not snippets:
            paras.append("No prior reporting was available for this section, so the draft relies on the instructions alone.")

        if shape in ("Table", "DataTable"):
            rows = ["| Item | Finding |", "| --- | --- |"]
            for i, s in enumerate(snippets or ["No data retrieved."], 1):
                rows.append(f"| {i} | {_first_sentence(s, 120)} |")
            paras = [paras[0], "\n".join(rows)]
        if limit:
            paras = paras[: int(limit)]
        return "\n\n".join(paras)
