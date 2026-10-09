"""WriterAgent: turns a section's instructions plus retrieved context into draft text."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..llm import LLMClient
from ..schema import Section
from ..templating import render
from .base import Agent, EventLog
from .context import ContextBundle

TABLE_SHAPES = {"Table", "DataTable"}


@dataclass
class Draft:
    text: str
    model: str
    replacements: dict[str, int] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0


def apply_terminology(text: str, mapping: dict[str, str]) -> tuple[str, dict[str, int]]:
    """Replace preferred-terminology keys with their values, whole-word and case-aware.

    This is deterministic on purpose: the model is asked to follow the glossary, but the
    glossary is enforced in code so a house style never depends on the model's compliance.
    """
    counts: dict[str, int] = {}
    # longest keys first so "tight fit" wins over "tight"
    for old in sorted(mapping, key=len, reverse=True):
        new = mapping[old]
        if not old or old.lower() == new.lower():
            continue
        pattern = re.compile(rf"(?<![A-Za-z]){re.escape(old)}(?![A-Za-z])", re.IGNORECASE)

        def sub(m: re.Match, new: str = new) -> str:
            return new[:1].upper() + new[1:] if m.group(0)[:1].isupper() else new

        text, n = pattern.subn(sub, text)
        if n:
            counts[old] = n
    return text, counts


class WriterAgent(Agent):
    name = "writer"

    def __init__(self, llm: LLMClient, log: EventLog | None = None, max_tokens: int = 1500) -> None:
        super().__init__(log)
        self.llm = llm
        self.max_tokens = max_tokens

    def system_prompt(self, section: Section) -> str:
        st = section.style
        lines = [
            "You are a report writer for a fashion house producing dress collection reports.",
            "Write only the body of the requested section: no preamble, no section heading.",
            f"Voice: {st.voice}. Formality: {st.formality}. Tense: {st.tense}. Person: {st.person}.",
            "Use only facts present in the provided context or the report variables. "
            "If the context does not support a claim, leave it out rather than guessing.",
        ]
        if st.max_paragraphs:
            lines.append(f"Use at most {st.max_paragraphs} paragraphs.")
        if st.citation_style == "Inline":
            lines.append("Cite context inline as [1], [2] matching the numbered context items.")
        elif st.citation_style == "Footnote":
            lines.append("Cite context with footnote markers and list them at the end.")
        if section.shape in TABLE_SHAPES:
            lines.append("Present the core data as a Markdown table, with a one-sentence lead-in.")
        if st.preferred_terminology:
            glossary = "; ".join(f"say '{new}' not '{old}'" for old, new in st.preferred_terminology.items())
            lines.append(f"House terminology: {glossary}.")
        return "\n".join(lines)

    def write(self, section: Section, variables: dict[str, Any], context: ContextBundle,
              revision_notes: list[str] | None = None) -> Draft:
        instructions, missing = render(section.instructions, variables)
        parts = [f"Section: {section.num} {section.title}", f"Instructions: {instructions}"]
        if context.snippets:
            parts.append("Context:\n" + "\n".join(
                f"[{i}] {s.text}" for i, s in enumerate(context.snippets, 1)))
        else:
            parts.append("Context: none available.")
        if revision_notes:
            parts.append("The previous draft was rejected by QA. Fix these issues:\n"
                         + "\n".join(f"- {n}" for n in revision_notes))
        meta = {
            "title": section.title, "shape": section.shape, "instructions": instructions,
            "snippets": [s.text for s in context.snippets], "max_paragraphs": section.style.max_paragraphs,
            "revision_notes": revision_notes or [],
        }
        resp = self.llm.complete(system=self.system_prompt(section), prompt="\n\n".join(parts),
                                 task="write", max_tokens=self.max_tokens, meta=meta)
        text, counts = apply_terminology(resp.text.strip(), section.style.preferred_terminology)
        self.log.emit(self.name, "drafted", section.num, chars=len(text), revision=bool(revision_notes),
                      terminology_fixes=sum(counts.values()), model=resp.model)
        return Draft(text=text, model=resp.model, replacements=counts,
                     input_tokens=resp.input_tokens, output_tokens=resp.output_tokens)
