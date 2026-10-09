"""Render a Report as Markdown or HTML (HTML-escaped; tables supported)."""
from __future__ import annotations

import html
import re

from .report import Report

_NOTE = {
    "needs_review": "This section did not meet the quality bar after revision and needs a human edit.",
    "escalated": "A required source returned nothing. A person must supply this section.",
    "manual": "This section is not AI-generated. Add the content manually.",
    "failed": "Generation failed for this section. Retry or write it manually.",
}


def _heading(depth: int) -> str:
    return "#" * min(2 + depth, 6)


def to_markdown(report: Report) -> str:
    out = [f"# {report.config_name}", ""]
    out.append(f"*Configuration {report.config_id} v{report.config_version} · generated {report.created_at}*")
    out.append("")
    for s in report.sections:
        out += [f"{_heading(s.depth)} {s.num} {s.title}", ""]
        if s.status in _NOTE:
            out += [f"> **Needs attention:** {_NOTE[s.status]}", ""]
        if s.content:
            out += [s.content, ""]
    if report.warnings or any(s.warnings for s in report.sections):
        out += ["## Generation notes", ""]
        out += [f"- {w}" for w in report.warnings]
        out += [f"- Section {s.num}: {w}" for s in report.sections for w in s.warnings]
    return "\n".join(out).rstrip() + "\n"


def _table_html(block: str) -> str:
    rows = [r.strip().strip("|") for r in block.splitlines() if r.strip()]
    cells = [[html.escape(c.strip()) for c in r.split("|")] for r in rows if not re.fullmatch(r"[\s:\-|]+", r)]
    if not cells:
        return ""
    head, body = cells[0], cells[1:]
    return ("<table><thead><tr>" + "".join(f"<th>{c}</th>" for c in head) + "</tr></thead><tbody>"
            + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in body) + "</tbody></table>")


def to_html(report: Report) -> str:
    parts = [f"<h1>{html.escape(report.config_name)}</h1>",
             f"<p class=\"meta\">Configuration {html.escape(report.config_id)} v{report.config_version}"
             f" &middot; generated {html.escape(report.created_at)}</p>"]
    for s in report.sections:
        lvl = min(2 + s.depth, 6)
        parts.append(f"<h{lvl}>{html.escape(s.num)} {html.escape(s.title)}</h{lvl}>")
        if s.status in _NOTE:
            parts.append(f"<p class=\"note\"><strong>Needs attention:</strong> {html.escape(_NOTE[s.status])}</p>")
        for block in re.split(r"\n\s*\n", s.content.strip()) if s.content else []:
            parts.append(_table_html(block) if block.lstrip().startswith("|") else f"<p>{html.escape(block)}</p>")
    style = ("body{font-family:Georgia,serif;max-width:46rem;margin:2rem auto;line-height:1.6;color:#222}"
             "table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:.4rem .6rem;text-align:left}"
             ".note{background:#fff7e6;padding:.5rem .8rem;border-left:3px solid #d97706}.meta{color:#666}")
    return (f"<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>{html.escape(report.config_name)}"
            f"</title><style>{style}</style></head><body>{''.join(parts)}</body></html>")
