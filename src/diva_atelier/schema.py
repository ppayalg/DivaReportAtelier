"""Typed view of a report configuration plus validation.

A configuration is the JSON document the dashboard edits and saves. It defines
the input variables collected before generation and the hierarchical sections
the agents write, each with writing instructions, retrieval sources, style and
tone, quality scoring and approvers.

The parsers are deliberately tolerant (the dashboard omits optional blocks) and
the validator is deliberately strict, so a bad configuration is caught before
any model call is spent on it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .templating import find_placeholders

OPERATORS = {"equals", "notEquals", "in", "notIn", "isNotNull", "isEmpty"}
VARIABLE_TYPES = {
    "FreeText", "Selection", "MultiSelection", "Date", "DateRange",
    "Boolean", "Number", "Collection",
}
SECTION_TYPES = {"ExecutiveSummary", "Content", "References", "Appendix"}
FALLBACKS = {"GenerateWithoutContext", "Warn", "Skip", "UseDefault", "Escalate"}
RETRIEVAL_MODES = {"Semantic", "Keyword", "Hybrid", "MetadataOnly"}
SCORE_DIMENSIONS = ("accuracy", "completeness", "structure", "clarity", "compliance")
TABLE_SHAPES = {"Table", "DataTable"}


@dataclass
class Condition:
    variable: str
    operator: str = "equals"
    value: Any = None


@dataclass
class Approver:
    role: str
    sla: int = 48
    order: int = 1
    required: bool = True


@dataclass
class Source:
    id: str
    index: str
    query: str
    priority: int = 1
    mode: str = "Semantic"
    topK: int = 3
    minScore: float = 0.0
    content_class: str = ""
    fallback: str = "GenerateWithoutContext"
    is_required: bool = False


@dataclass
class StyleTone:
    voice: str = "Technical"
    formality: str = "Standard"
    tense: str = "Mixed"
    person: str = "ThirdPerson"
    citation_style: str = "None"
    max_paragraphs: int | None = None
    preferred_terminology: dict[str, str] = field(default_factory=dict)


@dataclass
class QualityConfig:
    min_score: float = 90.0
    max_revisions: int = 3
    critical_issue_threshold: int = 0
    qa_instructions: str = ""
    validation_checklist: list[str] = field(default_factory=list)
    scoring_weights: dict[str, float] | None = None

    def weights(self) -> dict[str, float]:
        """Normalised weights; blank or invalid weights fall back to equal."""
        raw = self.scoring_weights or {}
        w = {d: float(raw.get(d, 0) or 0) for d in SCORE_DIMENSIONS}
        total = sum(w.values())
        if total <= 0:
            return {d: 1 / len(SCORE_DIMENSIONS) for d in SCORE_DIMENSIONS}
        return {d: v / total for d, v in w.items()}


@dataclass
class Variable:
    id: str
    name: str
    label: str
    type: str = "FreeText"
    sort: int = 0
    required: bool = True
    help: str = ""
    options: list[str] = field(default_factory=list)
    validation: dict[str, Any] = field(default_factory=dict)


@dataclass
class Section:
    id: str
    num: str
    title: str
    type: str = "Content"
    shape: str = "Narrative"
    seq: int = 0
    parent: str = ""
    required: bool = True
    generatable: bool = True
    instructions: str = ""
    sources: list[Source] = field(default_factory=list)
    approvers: list[Approver] = field(default_factory=list)
    condition: Condition | None = None
    style: StyleTone = field(default_factory=StyleTone)
    quality: QualityConfig = field(default_factory=QualityConfig)
    sample: str = ""


@dataclass
class ReportConfig:
    key: str
    id: str
    name: str
    version: int
    status: str
    type: str
    client: str
    country: str
    output: str
    style: str
    num_approvers_required: int
    global_approvers: list[Approver]
    variables: list[Variable]
    sections: list[Section]
    auto_approve_threshold: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def variable(self, name: str) -> Variable | None:
        return next((v for v in self.variables if v.name == name), None)

    def section(self, ref: str) -> Section | None:
        """Look a section up by id or by its printed number (e.g. 'ES', '3.2')."""
        return next((s for s in self.sections if s.id == ref or s.num == ref), None)


# ── parsing ──────────────────────────────────────────────────────────────

def _approver(d: dict) -> Approver:
    return Approver(role=d["role"], sla=int(d.get("sla", 48)), order=int(d.get("order", 1)),
                    required=bool(d.get("required", True)))


def _source(d: dict) -> Source:
    return Source(
        id=d.get("id", ""), index=d.get("index", ""), query=d.get("query", ""),
        priority=int(d.get("priority", 1)), mode=d.get("mode", "Semantic"),
        topK=int(d.get("topK", 3)), minScore=float(d.get("minScore", 0.0)),
        content_class=d.get("contentClass") or d.get("filter") or "",
        fallback=d.get("fallback", "GenerateWithoutContext"),
        is_required=bool(d.get("isRequired", False)),
    )


def _style(d: dict | None) -> StyleTone:
    d = d or {}
    mp = d.get("maxParagraphs")
    return StyleTone(
        voice=d.get("voice", "Technical"), formality=d.get("formality", "Standard"),
        tense=d.get("tense", "Mixed"), person=d.get("person", "ThirdPerson"),
        citation_style=d.get("citationStyle", "None"),
        max_paragraphs=int(mp) if mp not in (None, "") else None,
        preferred_terminology=dict(d.get("preferredTerminology") or {}),
    )


def _quality(d: dict | None) -> QualityConfig:
    d = d or {}
    return QualityConfig(
        min_score=float(d.get("minScore", 90)), max_revisions=int(d.get("maxRevisions", 3)),
        critical_issue_threshold=int(d.get("criticalIssueThreshold", 0)),
        qa_instructions=d.get("qaInstructions", ""),
        validation_checklist=[c for c in d.get("validationChecklist", []) if c],
        scoring_weights=d.get("scoringWeights") or None,
    )


def parse_config(raw: dict[str, Any], key: str = "") -> ReportConfig:
    """Parse a dashboard configuration dict. Raises KeyError/ValueError on malformed input."""
    variables = [
        Variable(id=v.get("id", f"v{i}"), name=v["name"], label=v.get("label", v["name"]),
                 type=v.get("type", "FreeText"), sort=int(v.get("sort", i)),
                 required=bool(v.get("required", True)), help=v.get("help", ""),
                 options=list(v.get("options") or []), validation=dict(v.get("validation") or {}))
        for i, v in enumerate(raw.get("variables", []), 1)
    ]
    sections: list[Section] = []
    for i, s in enumerate(raw.get("sections", []), 1):
        cond = s.get("condition")
        sections.append(Section(
            id=s.get("id", f"s{i}"), num=str(s.get("num", i)), title=s["title"],
            type=s.get("type", "Content"), shape=s.get("shape", "Narrative"),
            seq=int(s.get("seq", i)), parent=s.get("parent", "") or "",
            required=bool(s.get("required", True)), generatable=bool(s.get("generatable", True)),
            instructions=s.get("instructions") or s.get("writingInstructions") or "",
            sources=[_source(x) for x in s.get("sources", [])],
            approvers=[_approver(x) for x in s.get("approvers", [])],
            condition=Condition(cond["variable"], cond.get("operator", "equals"), cond.get("value"))
            if cond else None,
            style=_style(s.get("styleTone")), quality=_quality(s.get("qualityConfig")),
            sample=s.get("sampleHtml") or s.get("sample") or "",
        ))
    aat = raw.get("autoApproveThreshold")
    return ReportConfig(
        key=key, id=raw.get("id", key), name=raw["name"], version=int(raw.get("version", 1)),
        status=raw.get("status", "Draft"), type=raw.get("type", ""), client=raw.get("client", ""),
        country=raw.get("country", ""), output=raw.get("output", "docx"), style=raw.get("style", ""),
        num_approvers_required=int(raw.get("numApproversRequired", 1)),
        global_approvers=[_approver(a) for a in raw.get("globalApprovers", [])],
        variables=variables, sections=sections,
        auto_approve_threshold=float(aat) if aat not in (None, "") else None, raw=raw,
    )


# ── validation ───────────────────────────────────────────────────────────

@dataclass
class Issue:
    severity: str          # "error" blocks generation; "warning" does not
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "path": self.path, "message": self.message}


_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def validate_config(raw: dict[str, Any]) -> list[Issue]:
    """Return every problem found in a raw configuration (never raises)."""
    issues: list[Issue] = []

    def err(path: str, msg: str) -> None:
        issues.append(Issue("error", path, msg))

    def warn(path: str, msg: str) -> None:
        issues.append(Issue("warning", path, msg))

    for k in ("name", "variables", "sections"):
        if k not in raw:
            err(k, f"Missing required field '{k}'.")
    if any(i.severity == "error" for i in issues):
        return issues
    try:
        cfg = parse_config(raw)
    except (KeyError, TypeError, ValueError) as exc:
        err("config", f"Could not parse configuration: {exc!r}")
        return issues

    # variables
    names: set[str] = set()
    for v in cfg.variables:
        p = f"variables[{v.name}]"
        if not _NAME_RE.match(v.name):
            err(p, "Variable names must be snake_case (lowercase letters, digits, underscores).")
        if v.name in names:
            err(p, "Duplicate variable name.")
        names.add(v.name)
        if v.type not in VARIABLE_TYPES:
            err(p, f"Unknown variable type '{v.type}'.")
        if v.type in ("Selection", "MultiSelection") and not v.options:
            err(p, "Selection variables need at least one option.")

    # sections
    ids = {s.id for s in cfg.sections}
    if len(ids) != len(cfg.sections):
        err("sections", "Section ids must be unique.")
    nums = [s.num for s in cfg.sections]
    if len(set(nums)) != len(nums):
        warn("sections", "Section numbers are not unique.")
    if not any(s.type == "ExecutiveSummary" for s in cfg.sections):
        warn("sections", "No ExecutiveSummary section defined.")
    for s in cfg.sections:
        p = f"sections[{s.num}]"
        if s.type not in SECTION_TYPES:
            err(p, f"Unknown section type '{s.type}'.")
        if s.parent and s.parent not in ids:
            err(p, f"Parent '{s.parent}' does not exist.")
        if s.generatable and not s.instructions.strip():
            warn(p, "AI-generatable section has no writing instructions.")
        if s.condition:
            if s.condition.operator not in OPERATORS:
                err(p, f"Unknown condition operator '{s.condition.operator}'.")
            if s.condition.variable not in names:
                err(p, f"Condition uses undefined variable '{s.condition.variable}'.")
        for ph in find_placeholders(s.instructions):
            if ph not in names:
                err(p, f"Instructions use undefined variable {{{ph}}}.")
        for src in s.sources:
            sp = f"{p}.sources[{src.id}]"
            if src.mode not in RETRIEVAL_MODES:
                err(sp, f"Unknown retrieval mode '{src.mode}'.")
            if src.fallback not in FALLBACKS:
                err(sp, f"Unknown fallback '{src.fallback}'.")
            if not 0.0 <= src.minScore <= 1.0:
                err(sp, "minScore must be between 0 and 1.")
            if src.topK < 1:
                err(sp, "topK must be at least 1.")
            for ph in find_placeholders(src.query):
                if ph not in names:
                    err(sp, f"Query uses undefined variable {{{ph}}}.")
        w = s.quality.scoring_weights
        if w and abs(sum(float(x or 0) for x in w.values()) - 1.0) > 0.01:
            err(f"{p}.qualityConfig", "Scoring weights must sum to 1.0.")
        if not 0 <= s.quality.min_score <= 100:
            err(f"{p}.qualityConfig", "minScore must be between 0 and 100.")
        for a in s.approvers:
            if a.sla < 1:
                err(f"{p}.approvers[{a.role}]", "SLA must be at least 1 hour.")

    # cycles in the parent chain
    by_id = {s.id: s for s in cfg.sections}
    for s in cfg.sections:
        seen, cur = set(), s
        while cur and cur.parent:
            if cur.id in seen:
                err(f"sections[{s.num}]", "Section hierarchy contains a cycle.")
                break
            seen.add(cur.id)
            cur = by_id.get(cur.parent)

    if cfg.num_approvers_required < 1:
        err("numApproversRequired", "At least one approver is required.")
    return issues
