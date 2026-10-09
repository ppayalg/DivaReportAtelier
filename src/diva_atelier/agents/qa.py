"""QAAgent: scores a draft against the section's quality configuration.

Deterministic checks always run, so QA never silently passes just because a model call
failed. When ``use_llm_judge`` is on, a model also scores the draft and its dimension
scores are averaged with the deterministic ones.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..llm import LLMClient, LLMError
from ..retrieval import tokenize
from ..schema import SCORE_DIMENSIONS, Section
from ..templating import find_placeholders, render
from .base import Agent, EventLog
from .context import ContextBundle

TABLE_SHAPES = {"Table", "DataTable"}
_PENALTY = {"critical": 30.0, "major": 8.0, "minor": 2.0}
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?%?")


@dataclass
class QAIssue:
    severity: str            # critical | major | minor
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "code": self.code, "message": self.message}


@dataclass
class QAResult:
    score: float
    dimensions: dict[str, float]
    issues: list[QAIssue] = field(default_factory=list)
    passed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"score": round(self.score, 1), "passed": self.passed,
                "dimensions": {k: round(v, 3) for k, v in self.dimensions.items()},
                "issues": [i.to_dict() for i in self.issues]}

    @property
    def revision_notes(self) -> list[str]:
        return [i.message for i in self.issues if i.severity in ("critical", "major")]


def split_paragraphs(text: str) -> list[str]:
    """Prose paragraphs only: Markdown tables and headings do not count against paragraph limits."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    return [b for b in blocks if not b.lstrip().startswith(("|", "#"))]


def has_table(text: str) -> bool:
    return bool(re.search(r"^\s*\|.+\|\s*\n\s*\|[\s:\-|]+\|", text, re.MULTILINE))


def _clarity(text: str) -> float:
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text) if len(s.split()) > 2]
    if not sentences:
        return 1.0
    avg = sum(len(s.split()) for s in sentences) / len(sentences)
    if avg <= 28:
        return 1.0
    return max(0.6, 1.0 - (avg - 28) * (0.4 / 17))


class QAAgent(Agent):
    name = "qa"

    def __init__(self, llm: LLMClient | None = None, log: EventLog | None = None,
                 use_llm_judge: bool = False) -> None:
        super().__init__(log)
        self.llm = llm
        self.use_llm_judge = use_llm_judge and llm is not None

    def review(self, section: Section, text: str, variables: dict[str, Any],
               context: ContextBundle) -> QAResult:
        q = section.quality
        issues: list[QAIssue] = []
        stripped = text.strip()
        paragraphs = split_paragraphs(stripped)

        if not stripped:
            issues.append(QAIssue("critical", "empty_content", "The draft is empty."))
        for ph in find_placeholders(stripped):
            issues.append(QAIssue("critical", "unresolved_placeholder",
                                  f"Unresolved placeholder {{{ph}}} in the text; use the real value."))
        limit = section.style.max_paragraphs
        if limit and len(paragraphs) > limit:
            issues.append(QAIssue("major", "too_many_paragraphs",
                                  f"Uses {len(paragraphs)} paragraphs; the limit is {limit}."))
        if section.shape in TABLE_SHAPES and stripped and not has_table(stripped):
            issues.append(QAIssue("major", "missing_table", "This section must include a Markdown table."))
        forbidden = [k for k in section.style.preferred_terminology
                     if re.search(rf"(?<![A-Za-z]){re.escape(k)}(?![A-Za-z])", stripped, re.IGNORECASE)]
        for k in forbidden:
            issues.append(QAIssue("major", "forbidden_term",
                                  f"Use '{section.style.preferred_terminology[k]}' instead of '{k}'."))

        # accuracy: numbers in the draft should be traceable to context or variables
        grounding = " ".join([s.text for s in context.snippets] + [str(v) for v in variables.values()]
                             + [render(section.instructions, variables)[0]])
        known = {n.rstrip("%").replace(",", "") for n in _NUMBER.findall(grounding)}
        ungrounded = sorted({n for n in _NUMBER.findall(stripped)
                             if n.rstrip("%").replace(",", "") not in known})
        if ungrounded:
            issues.append(QAIssue("minor", "ungrounded_numbers",
                                  f"Figures not found in the context: {', '.join(ungrounded[:5])}."))

        # completeness: instruction coverage plus any validation checklist
        instr_terms = {t for t in tokenize(render(section.instructions, variables)[0]) if len(t) >= 5}
        body_terms = set(tokenize(stripped))
        coverage = len(instr_terms & body_terms) / len(instr_terms) if instr_terms else 1.0
        met = []
        for item in q.validation_checklist:
            terms = {t for t in tokenize(item) if len(t) >= 4}
            ok = not terms or len(terms & body_terms) / len(terms) >= 0.6
            met.append(ok)
            if not ok:
                issues.append(QAIssue("major", "checklist_unmet", f"Checklist item not evidenced: {item}"))
        completeness = (coverage + (sum(met) / len(met))) / 2 if met else coverage

        dims = {
            "accuracy": max(0.0, 1.0 - 0.15 * len(ungrounded)),
            "completeness": completeness,
            "structure": 0.0 if not stripped else max(0.0, 1.0 - 0.3 * any(i.code in ("too_many_paragraphs", "missing_table")
                                                                          for i in issues)),
            "clarity": _clarity(stripped) if stripped else 0.0,
            "compliance": max(0.0, 1.0 - 0.5 * sum(i.code == "unresolved_placeholder" for i in issues)
                              - 0.3 * len(forbidden)),
        }
        if self.use_llm_judge:
            dims = self._blend_with_judge(section, stripped, context, dims, issues)

        w = q.weights()
        score = 100.0 * sum(w[d] * dims[d] for d in SCORE_DIMENSIONS)
        score -= sum(_PENALTY[i.severity] for i in issues)
        score = max(0.0, min(100.0, score))
        criticals = sum(i.severity == "critical" for i in issues)
        passed = score >= q.min_score and criticals <= q.critical_issue_threshold
        self.log.emit(self.name, "reviewed", section.num, score=round(score, 1), passed=passed,
                      issues=[i.code for i in issues])
        return QAResult(score=score, dimensions=dims, issues=issues, passed=passed)

    def _blend_with_judge(self, section: Section, text: str, context: ContextBundle,
                          dims: dict[str, float], issues: list[QAIssue]) -> dict[str, float]:
        prompt = (
            "Score this report section from 0.0 to 1.0 on accuracy, completeness, structure, clarity and "
            "compliance. Reply with JSON only: {\"scores\": {...}, \"issues\": [\"...\"]}.\n"
            f"QA instructions: {section.quality.qa_instructions or 'none'}\n"
            f"Section instructions: {section.instructions}\n"
            f"Context:\n" + "\n".join(s.text for s in context.snippets) + f"\n\nDraft:\n{text}")
        try:
            resp = self.llm.complete(system="You are a strict report QA reviewer.", prompt=prompt, task="qa",
                                     max_tokens=500, temperature=0.0,
                                     meta={"checklist": section.quality.validation_checklist})
            m = re.search(r"\{.*\}", resp.text, re.DOTALL)
            data = json.loads(m.group(0)) if m else {}
            scores = data.get("scores", {})
            for d in SCORE_DIMENSIONS:
                if isinstance(scores.get(d), (int, float)):
                    dims[d] = (dims[d] + max(0.0, min(1.0, float(scores[d])))) / 2
            for msg in data.get("issues", []):
                issues.append(QAIssue("major", "judge_issue", str(msg)))
        except (LLMError, ValueError, TypeError) as exc:
            self.log.emit(self.name, "judge_failed", section.num, error=str(exc))
        return dims
