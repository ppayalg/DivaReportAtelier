"""ApprovalAgent: builds and runs the review workflow defined in the configuration.

Rules (all taken from the configuration screens):
  * Each included section with approvers gets one request per approver, run in `order`.
  * With auto-approve on, a section whose QA passed at or above the threshold skips review.
  * Global approvals open only after every required section approval is resolved.
  * The global stage needs ``numApproversRequired`` approvals; named roles must all sign off and
    any shortfall is filled by generic slots (role "*") that any reviewer may take.
  * A person cannot approve their own report or approve the same scope twice.
  * Rejecting needs a comment and moves the report to ``changes_requested``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ..schema import ReportConfig
from .base import Agent

ANY_ROLE = "*"
RESOLVED = {"approved", "auto_approved"}


class ApprovalError(ValueError):
    """The requested approval action is not allowed."""


@dataclass
class ApprovalRequest:
    id: str
    scope: str                    # "section" | "global"
    role: str
    order: int
    sla_hours: int
    due_at: str
    section_id: str | None = None
    status: str = "pending"       # pending | approved | rejected | auto_approved
    decided_by: str | None = None
    decided_at: str | None = None
    comment: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class ApprovalWorkflow:
    author: str
    requests: list[ApprovalRequest] = field(default_factory=list)

    @property
    def status(self) -> str:
        if any(r.status == "rejected" for r in self.requests):
            return "changes_requested"
        if all(r.status in RESOLVED for r in self.requests):
            return "approved"
        return "in_review"

    def actionable(self, req: ApprovalRequest) -> bool:
        if req.status != "pending":
            return False
        same = [r for r in self.requests if r.scope == req.scope and r.section_id == req.section_id]
        if any(r.order < req.order and r.status not in RESOLVED for r in same):
            return False
        if req.scope == "global":
            return all(r.status in RESOLVED for r in self.requests if r.scope == "section")
        return True

    def overdue(self, now: datetime | None = None) -> list[ApprovalRequest]:
        now = now or datetime.now(timezone.utc)
        return [r for r in self.requests
                if r.status == "pending" and datetime.fromisoformat(r.due_at) < now]

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "author": self.author,
                "requests": [dict(r.to_dict(), actionable=self.actionable(r)) for r in self.requests]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ApprovalWorkflow":
        wf = cls(author=d.get("author", ""))
        for r in d.get("requests", []):
            r = {k: v for k, v in r.items() if k != "actionable"}
            wf.requests.append(ApprovalRequest(**r))
        return wf


class ApprovalAgent(Agent):
    name = "approval"

    def build(self, config: ReportConfig, section_results: list[dict[str, Any]], author: str,
              auto_approve: bool = False, now: datetime | None = None) -> ApprovalWorkflow:
        """``section_results``: dicts with section_id, status and qa_score for included sections."""
        now = now or datetime.now(timezone.utc)
        wf = ApprovalWorkflow(author=author)
        n = 0

        def add(scope: str, role: str, order: int, sla: int, section_id: str | None, status: str = "pending") -> None:
            nonlocal n
            n += 1
            wf.requests.append(ApprovalRequest(
                id=f"ap-{n}", scope=scope, role=role, order=order, sla_hours=sla,
                due_at=(now + timedelta(hours=sla)).isoformat(timespec="seconds"),
                section_id=section_id, status=status))

        threshold = config.auto_approve_threshold if config.auto_approve_threshold is not None else 90.0
        by_id = {r["section_id"]: r for r in section_results}
        for s in config.sections:
            res = by_id.get(s.id)
            if not res:
                continue
            for a in sorted(s.approvers, key=lambda x: x.order):
                if not a.required:
                    continue
                auto = auto_approve and res.get("status") == "passed" and (res.get("qa_score") or 0) >= threshold
                add("section", a.role, a.order, a.sla, s.id, "auto_approved" if auto else "pending")

        named = [a for a in sorted(config.global_approvers, key=lambda x: x.order) if a.required]
        for a in named:
            add("global", a.role, a.order, a.sla, None)
        top = max((a.order for a in named), default=0)
        for i in range(max(0, config.num_approvers_required - len(named))):
            add("global", ANY_ROLE, top + 1, 48, None)
        self.log.emit(self.name, "workflow_built", requests=len(wf.requests))
        return wf

    def act(self, wf: ApprovalWorkflow, request_id: str, user: str, role: str, action: str,
            comment: str = "", now: datetime | None = None) -> ApprovalRequest:
        req = next((r for r in wf.requests if r.id == request_id), None)
        if req is None:
            raise ApprovalError(f"No approval request '{request_id}'.")
        if action not in ("approve", "reject"):
            raise ApprovalError("Action must be 'approve' or 'reject'.")
        if not wf.actionable(req):
            raise ApprovalError("This request is not open yet or has already been decided.")
        if req.role != ANY_ROLE and req.role != role:
            raise ApprovalError(f"This step needs the {req.role} role.")
        if user == wf.author:
            raise ApprovalError("Authors cannot review their own report.")
        if any(r.decided_by == user and r.scope == req.scope and r.section_id == req.section_id
               and r.status == "approved" for r in wf.requests):
            raise ApprovalError(f"{user} has already approved this step.")
        if action == "reject" and not comment.strip():
            raise ApprovalError("A comment is required when requesting changes.")
        req.status = "approved" if action == "approve" else "rejected"
        req.decided_by, req.comment = user, comment
        req.decided_at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
        self.log.emit(self.name, "decision", req.section_id, request=req.id, action=action, by=user)
        return req
