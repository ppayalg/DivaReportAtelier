"""Retrieval interface. Swap InMemoryRetriever for a vector store without touching agents."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Document:
    id: str
    index: str
    text: str
    content_class: str = "Narrative"      # Narrative | Table | Image | RawDocument
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Hit:
    doc: Document
    score: float                           # 0..1


class Retriever(Protocol):
    def search(self, *, index: str, query: str, mode: str = "Semantic", top_k: int = 3,
               min_score: float = 0.0, content_class: str = "") -> list[Hit]:
        ...
