"""ContextAgent: runs a section's retrieval sources in priority order and applies fallbacks."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..retrieval import Retriever
from ..schema import Section
from ..templating import render
from .base import Agent, EventLog


@dataclass
class Snippet:
    doc_id: str
    text: str
    score: float
    source_id: str
    index: str


@dataclass
class ContextBundle:
    snippets: list[Snippet] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    escalate: bool = False
    used_default: bool = False


class ContextAgent(Agent):
    name = "context"

    def __init__(self, retriever: Retriever, log: EventLog | None = None) -> None:
        super().__init__(log)
        self.retriever = retriever

    def gather(self, section: Section, variables: dict[str, Any]) -> ContextBundle:
        bundle = ContextBundle()
        seen: set[str] = set()
        for src in sorted(section.sources, key=lambda s: s.priority):
            query, missing = render(src.query, variables)
            hits = [] if missing else self.retriever.search(
                index=src.index, query=query, mode=src.mode, top_k=src.topK,
                min_score=src.minScore, content_class=src.content_class)
            if missing:
                bundle.warnings.append(
                    f"Source '{src.id}' skipped: query uses unset variable(s) {', '.join(missing)}.")
            fresh = [h for h in hits if h.doc.id not in seen]
            for h in fresh:
                seen.add(h.doc.id)
                bundle.snippets.append(Snippet(h.doc.id, h.doc.text, h.score, src.id, src.index))
            self.log.emit(self.name, "retrieved", section.num, source=src.id, index=src.index,
                          hits=len(fresh), mode=src.mode)
            if fresh:
                continue
            fb = src.fallback
            if fb == "Skip":
                continue
            if fb == "Warn":
                bundle.warnings.append(f"Source '{src.id}' returned nothing for section {section.num}.")
            elif fb == "UseDefault":
                if section.sample:
                    bundle.snippets.append(Snippet(f"default:{section.id}", section.sample, 0.0, src.id, "default"))
                    bundle.used_default = True
                else:
                    bundle.warnings.append(f"Source '{src.id}' returned nothing and no default content is set.")
            elif fb == "Escalate":
                bundle.escalate = True
                bundle.warnings.append(f"Source '{src.id}' returned nothing; escalated for human input.")
            elif src.is_required:        # GenerateWithoutContext on a required source still deserves a flag
                bundle.warnings.append(f"Required source '{src.id}' returned nothing; generated without context.")
        return bundle
