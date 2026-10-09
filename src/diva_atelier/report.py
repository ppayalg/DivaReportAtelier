"""Report data model produced by the orchestrator."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SectionResult:
    section_id: str
    num: str
    title: str
    depth: int
    status: str            # passed | needs_review | escalated | manual | failed
    content: str = ""
    qa: dict[str, Any] | None = None
    context: list[dict[str, Any]] = field(default_factory=list)
    attempts: int = 0
    warnings: list[str] = field(default_factory=list)
    model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Report:
    id: str
    config_key: str
    config_id: str
    config_name: str
    config_version: int
    variables: dict[str, Any]
    created_at: str
    status: str                                   # ready_for_review | needs_attention
    sections: list[SectionResult] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    workflow: dict[str, Any] | None = None
    usage: dict[str, int] = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["sections"] = [s.to_dict() for s in self.sections]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Report":
        d = dict(d)
        d["sections"] = [SectionResult(**s) for s in d.get("sections", [])]
        return cls(**d)
