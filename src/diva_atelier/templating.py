"""{variable} substitution used by writing instructions and retrieval queries."""
from __future__ import annotations

import re
from typing import Any

_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")


def find_placeholders(text: str) -> list[str]:
    """Names of every {placeholder} in text, in order of first appearance."""
    seen: list[str] = []
    for m in _PLACEHOLDER.finditer(text or ""):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def format_value(value: Any) -> str:
    """Human-readable form of a variable value for use inside prose."""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, dict) and ("start" in value or "end" in value):
        return f"{value.get('start', '')} – {value.get('end', '')}".strip(" –")
    if isinstance(value, (list, tuple)):
        return ", ".join(format_value(v) for v in value)
    return "" if value is None else str(value)


def render(text: str, variables: dict[str, Any]) -> tuple[str, list[str]]:
    """Substitute {name} placeholders. Returns (rendered_text, missing_names).

    Unknown placeholders are left in place so QA can flag them rather than the
    model silently inventing a value.
    """
    missing: list[str] = []

    def sub(m: re.Match) -> str:
        name = m.group(1)
        if name in variables and variables[name] not in (None, ""):
            return format_value(variables[name])
        if name not in missing:
            missing.append(name)
        return m.group(0)

    return _PLACEHOLDER.sub(sub, text or ""), missing
