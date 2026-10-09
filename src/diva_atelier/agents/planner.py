"""PlannerAgent: validates inputs and decides which sections the run will produce."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..conditions import evaluate
from ..schema import ReportConfig, Section, Variable
from .base import Agent

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_MONTH = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")


@dataclass
class PlanStep:
    section: Section
    included: bool
    reason: str = ""
    depth: int = 0


@dataclass
class Plan:
    steps: list[PlanStep] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def included(self) -> list[PlanStep]:
        return [s for s in self.steps if s.included]


def _to_date(text: str) -> date | None:
    try:
        parts = [int(p) for p in text.split("-")]
        return date(parts[0], parts[1], parts[2] if len(parts) > 2 else 1)
    except (ValueError, IndexError):
        return None


def check_variable(v: Variable, value: Any, today: date | None = None) -> str | None:
    """Return an error message, or None when the value is acceptable."""
    today = today or date.today()
    blank = value in (None, "", [], {})
    if blank:
        return f"'{v.label}' is required." if v.required else None
    t = v.type
    if t == "FreeText" and not isinstance(value, str):
        return f"'{v.label}' must be text."
    if t == "Number":
        try:
            float(value)
        except (TypeError, ValueError):
            return f"'{v.label}' must be a number."
    if t == "Boolean" and not (isinstance(value, bool) or str(value).strip().lower() in
                               {"yes", "no", "true", "false", "y", "n", "1", "0"}):
        return f"'{v.label}' must be Yes or No."
    if t == "Selection" and value not in v.options:
        return f"'{v.label}' must be one of: {', '.join(v.options)}."
    if t == "MultiSelection":
        items = value if isinstance(value, list) else [value]
        bad = [i for i in items if i not in v.options]
        if bad:
            return f"'{v.label}' has invalid choices: {', '.join(map(str, bad))}."
    if t == "Collection" and not isinstance(value, list):
        return f"'{v.label}' must be a list."
    if t == "Date":
        if not isinstance(value, str) or not _ISO_MONTH.match(value) or not _to_date(value):
            return f"'{v.label}' must be an ISO date (YYYY-MM or YYYY-MM-DD)."
        if v.validation.get("maxDate") == "today" and _to_date(value) > today:
            return f"'{v.label}' cannot be in the future."
    if t == "DateRange":
        if not (isinstance(value, dict) and _ISO_DATE.match(str(value.get("start", "")))
                and _ISO_DATE.match(str(value.get("end", "")))):
            return f"'{v.label}' needs start and end dates (YYYY-MM-DD)."
        s, e = _to_date(value["start"]), _to_date(value["end"])
        if not s or not e:
            return f"'{v.label}' contains an invalid date."
        if s > e:
            return f"'{v.label}' start date is after its end date."
        span = v.validation.get("maxSpanDays")
        if span and (e - s).days > int(span):
            return f"'{v.label}' spans {(e - s).days} days; the maximum is {span}."
        if v.validation.get("maxDate") == "today" and e > today:
            return f"'{v.label}' cannot end in the future."
    return None


class PlannerAgent(Agent):
    name = "planner"

    def plan(self, config: ReportConfig, variables: dict[str, Any], today: date | None = None) -> Plan:
        plan = Plan()
        for v in sorted(config.variables, key=lambda x: x.sort):
            msg = check_variable(v, variables.get(v.name), today)
            if msg:
                plan.errors.append(msg)
        known = {v.name for v in config.variables}
        for extra in sorted(set(variables) - known):
            plan.warnings.append(f"Variable '{extra}' is not defined in this configuration and was ignored.")
        if plan.errors:
            self.log.emit(self.name, "inputs_invalid", errors=plan.errors)
            return plan

        children: dict[str, list[Section]] = {}
        for s in config.sections:
            children.setdefault(s.parent, []).append(s)
        for lst in children.values():
            lst.sort(key=lambda s: s.seq)

        def walk(parent_id: str, depth: int, parent_ok: bool) -> None:
            for s in children.get(parent_id, []):
                if not parent_ok:
                    plan.steps.append(PlanStep(s, False, "parent section excluded", depth))
                    walk(s.id, depth + 1, False)
                    continue
                ok = evaluate(s.condition, variables)
                reason = "" if ok else (
                    f"condition not met: {s.condition.variable} {s.condition.operator} {s.condition.value}")
                plan.steps.append(PlanStep(s, ok, reason, depth))
                if ok and not s.generatable:
                    plan.warnings.append(f"Section {s.num} '{s.title}' is not AI-generatable and needs manual input.")
                walk(s.id, depth + 1, ok)

        walk("", 0, True)
        self.log.emit(self.name, "planned", included=len(plan.included), excluded=len(plan.steps) - len(plan.included))
        return plan
