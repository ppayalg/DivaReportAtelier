"""Conditional section inclusion ("show 3.2 only if leachate was sampled")."""
from __future__ import annotations

from typing import Any

from .schema import Condition

_TRUE = {"yes", "true", "y", "1"}
_FALSE = {"no", "false", "n", "0"}


def _norm(value: Any) -> str:
    """Normalise so Boolean True, 'Yes', 'true' and 'TRUE' all compare equal."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = str(value).strip().lower() if value is not None else ""
    if text in _TRUE:
        return "yes"
    if text in _FALSE:
        return "no"
    return text


def _as_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [_norm(v) for v in value]
    return [_norm(v) for v in str(value or "").split(",") if v.strip()]


def evaluate(cond: Condition | None, variables: dict[str, Any]) -> bool:
    """True when the section should be included. No condition means always."""
    if cond is None:
        return True
    actual = variables.get(cond.variable)
    op = cond.operator
    if op == "isNotNull":
        return actual not in (None, "", [], {})
    if op == "isEmpty":
        return actual in (None, "", [], {})
    if op == "equals":
        return _norm(actual) == _norm(cond.value)
    if op == "notEquals":
        return _norm(actual) != _norm(cond.value)
    wanted = _as_list(cond.value)
    have = _as_list(actual) if isinstance(actual, (list, tuple, set)) else [_norm(actual)]
    if op == "in":
        return any(h in wanted for h in have)
    if op == "notIn":
        return not any(h in wanted for h in have)
    raise ValueError(f"Unknown condition operator: {op}")
