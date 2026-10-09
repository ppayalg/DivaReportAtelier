"""Application service: the one place that wires store, agents and models together.

The HTTP server and the CLI are thin shells over this class, so behaviour is identical
whichever way it is invoked.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .agents import (ApprovalAgent, ApprovalWorkflow, GenerationError, InsightsAgent,
                     ReportOrchestrator, apply_recommendation)
from .llm import LLMClient, get_llm
from .render import to_html, to_markdown
from .report import Report
from .retrieval import Retriever, load_knowledge
from .schema import parse_config, validate_config
from .store import JsonStore


class NotFound(LookupError):
    pass


class ConfigInvalid(ValueError):
    def __init__(self, issues: list[dict[str, str]]):
        super().__init__("Configuration is invalid.")
        self.issues = issues


class AtelierService:
    def __init__(self, store: JsonStore, retriever: Retriever, llm: LLMClient, *, use_llm_judge: bool = False):
        self.store, self.retriever, self.llm = store, retriever, llm
        self.orchestrator = ReportOrchestrator(llm, retriever, use_llm_judge=use_llm_judge)

    @classmethod
    def from_env(cls, root: str | Path | None = None) -> "AtelierService":
        """Build from environment: DIVA_DATA_DIR, DIVA_KNOWLEDGE_DIR, DIVA_LLM_PROVIDER."""
        base = Path(root or os.environ.get("DIVA_DATA_DIR", "var"))
        store = JsonStore(base)
        seed = Path(os.environ.get("DIVA_SEED_CONFIGS", "configs"))
        if seed.is_dir() and not store.list_configs():       # first run: copy the shipped configurations
            import json
            for p in sorted(seed.glob("*.json")):
                store.save_config(p.stem, json.loads(p.read_text(encoding="utf-8")))
        knowledge = Path(os.environ.get("DIVA_KNOWLEDGE_DIR", "data/knowledge"))
        retriever = load_knowledge(knowledge) if knowledge.is_dir() else load_knowledge(base / "knowledge")
        provider = os.environ.get("DIVA_LLM_PROVIDER", "mock")
        return cls(store, retriever, get_llm(provider), use_llm_judge=provider != "mock")

    # configurations
    def validate(self, config: dict[str, Any]) -> list[dict[str, str]]:
        return [i.to_dict() for i in validate_config(config)]

    def save_config(self, key: str, config: dict[str, Any]) -> list[dict[str, str]]:
        issues = self.validate(config)
        if any(i["severity"] == "error" for i in issues):
            raise ConfigInvalid(issues)
        self.store.save_config(key, config)
        return issues

    def _config(self, key: str) -> dict[str, Any]:
        raw = self.store.get_config(key)
        if raw is None:
            raise NotFound(f"No configuration '{key}'.")
        return raw

    # reports
    def generate(self, key: str, variables: dict[str, Any], *, author: str = "system",
                 auto_approve: bool = False) -> dict[str, Any]:
        raw = self._config(key)
        report = self.orchestrator.generate(raw, variables, key=key, author=author, auto_approve=auto_approve)
        data = report.to_dict()
        self.store.save_report(data)
        return data

    def get_report(self, report_id: str) -> dict[str, Any]:
        data = self.store.get_report(report_id)
        if data is None:
            raise NotFound(f"No report '{report_id}'.")
        return data

    def render(self, report_id: str, fmt: str) -> tuple[str, str]:
        report = Report.from_dict(self.get_report(report_id))
        if fmt == "html":
            return to_html(report), "text/html; charset=utf-8"
        if fmt in ("md", "markdown"):
            return to_markdown(report), "text/markdown; charset=utf-8"
        raise ValueError("format must be 'html' or 'md'")

    def decide(self, report_id: str, request_id: str, user: str, role: str, action: str,
               comment: str = "") -> dict[str, Any]:
        data = self.get_report(report_id)
        wf = ApprovalWorkflow.from_dict(data["workflow"])
        ApprovalAgent().act(wf, request_id, user, role, action, comment)
        data["workflow"] = wf.to_dict()
        self.store.save_report(data)
        return data["workflow"]

    # feedback and insights
    def add_feedback(self, key: str, item: dict[str, Any]) -> dict[str, Any]:
        self._config(key)
        if item.get("type") not in ("comment", "edit"):
            raise ValueError("Feedback type must be 'comment' or 'edit'.")
        if not item.get("section"):
            raise ValueError("Feedback needs a 'section'.")
        if item["type"] == "comment" and not item.get("text"):
            raise ValueError("A comment needs 'text'.")
        if item["type"] == "edit" and not (item.get("before") and item.get("after")):
            raise ValueError("An edit needs 'before' and 'after'.")
        return self.store.add_feedback(key, item)

    def insights(self, key: str) -> list[dict[str, Any]]:
        config = parse_config(self._config(key), key)
        recs = InsightsAgent().analyze(config, self.store.list_feedback(key))
        return [r.to_dict() for r in recs]

    def apply_insight(self, key: str, rec_id: str) -> dict[str, Any]:
        rec = next((r for r in self.insights(key) if r["id"] == rec_id), None)
        if rec is None:
            raise NotFound(f"No open recommendation '{rec_id}' for '{key}'.")
        updated = apply_recommendation(self._config(key), rec)
        updated["consumedFeedback"] = sorted(set(updated.get("consumedFeedback", [])) | set(rec["patternBasis"]))
        self.save_config(key, updated)
        return updated
