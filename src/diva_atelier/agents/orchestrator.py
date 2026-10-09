"""ReportOrchestrator: runs plan -> context -> write -> QA (with revisions) -> approvals."""
from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

from ..llm import LLMClient, LLMError
from ..report import Report, SectionResult
from ..retrieval import Retriever
from ..schema import ReportConfig, parse_config, validate_config
from .approval import ApprovalAgent
from .base import EventLog
from .context import ContextAgent
from .planner import Plan, PlanStep, PlannerAgent
from .qa import QAAgent
from .writer import WriterAgent


class GenerationError(ValueError):
    """The configuration or the inputs are not valid; ``problems`` lists every reason."""

    def __init__(self, message: str, problems: list[str]):
        super().__init__(message)
        self.problems = problems


class ReportOrchestrator:
    def __init__(self, llm: LLMClient, retriever: Retriever, *, use_llm_judge: bool = False,
                 max_workers: int = 4, id_factory: Callable[[], str] | None = None) -> None:
        self.llm, self.retriever = llm, retriever
        self.use_llm_judge = use_llm_judge
        self.max_workers = max_workers
        self.id_factory = id_factory or (lambda: "GR-" + uuid.uuid4().hex[:8].upper())

    def generate(self, config: ReportConfig | dict[str, Any], variables: dict[str, Any], *,
                 key: str = "", author: str = "system", auto_approve: bool = False) -> Report:
        if isinstance(config, dict):
            errors = [f"{i.path}: {i.message}" for i in validate_config(config) if i.severity == "error"]
            if errors:
                raise GenerationError("Configuration is invalid.", errors)
            config = parse_config(config, key)

        log = EventLog()
        planner, context = PlannerAgent(log), ContextAgent(self.retriever, log)
        writer, qa = WriterAgent(self.llm, log), QAAgent(self.llm, log, self.use_llm_judge)

        plan: Plan = planner.plan(config, variables)
        if plan.errors:
            raise GenerationError("Inputs are invalid.", plan.errors)

        steps = plan.included
        with ThreadPoolExecutor(max_workers=max(1, self.max_workers)) as pool:
            results = list(pool.map(lambda st: self._run_section(st, variables, context, writer, qa, log), steps))

        usage = {"input_tokens": 0, "output_tokens": 0}
        for r, _u in results:
            usage["input_tokens"] += _u[0]
            usage["output_tokens"] += _u[1]
        sections = [r for r, _ in results]

        wf = ApprovalAgent(log).build(
            config, [{"section_id": r.section_id, "status": r.status, "qa_score": (r.qa or {}).get("score")}
                     for r in sections], author=author, auto_approve=auto_approve)
        ok = all(r.status == "passed" for r in sections)
        report = Report(
            id=self.id_factory(), config_key=config.key, config_id=config.id, config_name=config.name,
            config_version=config.version, variables=variables,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            status="ready_for_review" if ok else "needs_attention", sections=sections,
            skipped=[{"section": f"{s.section.num} {s.section.title}", "reason": s.reason}
                     for s in plan.steps if not s.included],
            warnings=plan.warnings, events=[e.to_dict() for e in log.events()],
            workflow=wf.to_dict(), usage=usage)
        log.emit("orchestrator", "finished", report=report.id, status=report.status)
        report.events = [e.to_dict() for e in log.events()]
        return report

    def _run_section(self, step: PlanStep, variables: dict[str, Any], context: ContextAgent,
                     writer: WriterAgent, qa: QAAgent, log: EventLog) -> tuple[SectionResult, tuple[int, int]]:
        s = step.section
        res = SectionResult(section_id=s.id, num=s.num, title=s.title, depth=step.depth, status="passed")
        tokens = [0, 0]
        if not s.generatable:
            res.status = "manual"
            return res, (0, 0)
        try:
            bundle = context.gather(s, variables)
            res.context = [{"doc_id": x.doc_id, "score": x.score, "source_id": x.source_id} for x in bundle.snippets]
            res.warnings += bundle.warnings
            if bundle.escalate:
                res.status = "escalated"
                log.emit("orchestrator", "escalated", s.num)
                return res, (0, 0)

            notes: list[str] | None = None
            result = None
            for attempt in range(1, s.quality.max_revisions + 2):
                draft = writer.write(s, variables, bundle, notes)
                tokens[0] += draft.input_tokens
                tokens[1] += draft.output_tokens
                result = qa.review(s, draft.text, variables, bundle)
                res.content, res.attempts, res.model, res.qa = draft.text, attempt, draft.model, result.to_dict()
                if result.passed:
                    break
                notes = result.revision_notes or ["Improve overall quality."]
                log.emit("orchestrator", "revision_requested", s.num, attempt=attempt, notes=notes)
            if result is not None and not result.passed:
                res.status = "needs_review"
        except LLMError as exc:
            res.status = "failed"
            res.warnings.append(f"Model call failed: {exc}")
            log.emit("orchestrator", "section_failed", s.num, error=str(exc))
        return res, (tokens[0], tokens[1])
