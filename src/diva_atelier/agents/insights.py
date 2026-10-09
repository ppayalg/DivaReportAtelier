"""InsightsAgent: mines reviewer comments and writer edits for recurring corrections.

Every time a person fixes the same thing in generated reports, the configuration is the thing
that should change. This agent finds those repeats and proposes the exact configuration edit,
in the shape the dashboard's Configuration Insights screen displays.

Patterns detected (all deterministic, so results are explainable and testable):
  * terminology     the same word or phrase replaced in several edits (or "use 'x' not 'y'" comments)
  * required content the same sentence appended to a section in several reports
  * paragraph limit  repeated reviewer comments that a paragraph limit is too tight

``apply_recommendation`` then writes an accepted recommendation into the configuration.
"""
from __future__ import annotations

import copy
import difflib
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ..retrieval import tokenize
from ..schema import ReportConfig
from .base import Agent

_USE_NOT = re.compile(r"[Uu]se\s+['\"“‘]([^'\"”’]+)['\"”’]\s+not\s+['\"“‘]([^'\"”’]+)['\"”’]")
_URGENT = re.compile(r"\b(again|always|every time|missing|must)\b", re.IGNORECASE)
_PARA_LIMIT = re.compile(r"paragraph", re.IGNORECASE)
_TOO_TIGHT = re.compile(r"(too tight|too restrictive|limit)", re.IGNORECASE)


@dataclass
class Recommendation:
    id: str
    section: str
    section_title: str
    priority: str
    title: str
    summary: str
    confidence: float
    pattern_basis: list[str]
    pattern_tags: list[str]
    thread: list[dict[str, Any]]
    field: str
    label: str
    before: str
    after: str
    status: str = "open"

    def to_dict(self) -> dict[str, Any]:
        """Dashboard (INTEL_DATA) shape."""
        return {
            "id": self.id, "type": "recommendation", "status": self.status, "priority": self.priority,
            "section": self.section, "sectionTitle": self.section_title, "title": self.title,
            "summary": self.summary, "confidence": round(self.confidence, 2),
            "patternBasis": self.pattern_basis, "patternTags": self.pattern_tags,
            "feedbackThread": self.thread,
            "aiChange": {"field": self.field, "label": self.label, "before": self.before, "after": self.after},
        }


def _rec_id(field_path: str, after: str) -> str:
    return "rec-" + hashlib.sha1(f"{field_path}|{after}".encode()).hexdigest()[:8]


def _initials(name: str) -> str:
    return "".join(p[0] for p in name.split()[:2]).upper()


def _thread_item(fb: dict[str, Any]) -> dict[str, Any]:
    item = {"type": fb["type"], "author": fb.get("author", ""), "role": fb.get("role", ""),
            "date": fb.get("date", ""), "avatar": _initials(fb.get("author", ""))}
    if fb["type"] == "edit":
        item.update(before=fb.get("before", ""), after=fb.get("after", ""))
    else:
        item["text"] = fb.get("text", "")
    return item


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9'’-]+", text)


def word_replacements(before: str, after: str) -> list[tuple[str, str]]:
    """Small phrase substitutions between two texts, e.g. ('cheap', 'accessibly priced')."""
    b, a = _words(before), _words(after)
    sm = difflib.SequenceMatcher(a=[w.lower() for w in b], b=[w.lower() for w in a], autojunk=False)
    out = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "replace" and 1 <= i2 - i1 <= 3 and 1 <= j2 - j1 <= 4:
            out.append((" ".join(b[i1:i2]).lower(), " ".join(a[j1:j2]).lower()))
    return out


def _appended(before: str, after: str) -> str:
    """Text added after the original, or '' when the edit was not a pure append."""
    b, a = " ".join(before.split()), " ".join(after.split())
    return a[len(b):].strip() if b and a.startswith(b) and len(a) > len(b) else ""


def _sig(text: str) -> set[str]:
    return {t for t in tokenize(text) if len(t) > 3}


def _jaccard(x: set[str], y: set[str]) -> float:
    return len(x & y) / len(x | y) if x and y else 0.0


class InsightsAgent(Agent):
    name = "insights"

    def __init__(self, min_occurrences: int = 2, log=None) -> None:
        super().__init__(log)
        self.min_occurrences = min_occurrences

    def analyze(self, config: ReportConfig, feedback: list[dict[str, Any]]) -> list[Recommendation]:
        consumed = set(config.raw.get("consumedFeedback", []))     # feedback already acted on
        feedback = [dict(f, id=f.get("id", f"fb-{i}")) for i, f in enumerate(feedback, 1)]
        feedback = [f for f in feedback if f["id"] not in consumed]
        recs = (self._terminology(config, feedback) + self._required_content(config, feedback)
                + self._paragraph_limit(config, feedback))
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        recs.sort(key=lambda r: (order[r.priority], -r.confidence, r.id))
        self.log.emit(self.name, "analyzed", feedback=len(feedback), recommendations=len(recs))
        return recs

    # ── helpers ──
    def _section_title(self, config: ReportConfig, ref: str) -> str:
        s = config.section(ref)
        return s.title if s else ref

    def _top_section(self, items: list[dict[str, Any]]) -> str:
        return Counter(i.get("section", "") for i in items).most_common(1)[0][0]

    # ── terminology ──
    def _terminology(self, config: ReportConfig, feedback: list[dict[str, Any]]) -> list[Recommendation]:
        votes: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for fb in feedback:
            if fb["type"] == "edit":
                for pair in set(word_replacements(fb.get("before", ""), fb.get("after", ""))):
                    votes.setdefault(pair, []).append(fb)
            elif fb["type"] == "comment":
                m = _USE_NOT.search(fb.get("text", ""))
                if m:
                    votes.setdefault((m.group(2).lower(), m.group(1).lower()), []).append(fb)
        recs = []
        for (old, new), items in votes.items():
            if len(items) < self.min_occurrences:
                continue
            ref = self._top_section(items)
            sec = config.section(ref)
            current = dict(sec.style.preferred_terminology) if sec else {}
            if old in {k.lower() for k in current}:
                continue
            merged = dict(current)
            merged[old] = new
            field_path = f"sections[{ref}].styleTone.preferredTerminology"
            after = json.dumps(merged, ensure_ascii=False)
            reports = len({i.get("reportRun", i["id"]) for i in items})
            recs.append(Recommendation(
                id=_rec_id(field_path, after), section=ref, section_title=self._section_title(config, ref),
                priority="high", title=f"Update Style & Tone: add '{old}' → '{new}' to preferred terminology",
                summary=(f"Reviewers and writers replaced '{old}' with '{new}' {len(items)} times across "
                         f"{reports} report(s). Add it to the glossary so the writer agent applies it automatically."),
                confidence=min(0.99, 0.6 + 0.08 * len(items)),
                pattern_basis=[i["id"] for i in items],
                pattern_tags=["terminology", "style & tone", "brand language"],
                thread=[_thread_item(i) for i in items[:4]],
                field=field_path, label=f"Preferred Terminology — {self._section_title(config, ref)}",
                before=json.dumps(current, ensure_ascii=False), after=after))
        return recs

    # ── required content (repeated appends) ──
    def _required_content(self, config: ReportConfig, feedback: list[dict[str, Any]]) -> list[Recommendation]:
        adds = [(fb, _appended(fb.get("before", ""), fb.get("after", "")))
                for fb in feedback if fb["type"] == "edit"]
        adds = [(fb, t) for fb, t in adds if len(t.split()) >= 5]
        recs, used = [], set()
        for i, (fb, text) in enumerate(adds):
            if i in used:
                continue
            cluster = [(fb, text)]
            for j in range(i + 1, len(adds)):
                fb2, t2 = adds[j]
                if j not in used and fb2.get("section") == fb.get("section") and _jaccard(_sig(text), _sig(t2)) >= 0.3:
                    cluster.append((fb2, t2))
                    used.add(j)
            used.add(i)
            if len(cluster) < self.min_occurrences:
                continue
            ref = fb.get("section", "")
            sec = config.section(ref)
            if not sec:
                continue
            example = min((t for _, t in cluster), key=len)
            if example in sec.instructions:
                continue
            field_path = f"sections[{ref}].writingInstructions"
            after = f"{sec.instructions.rstrip()} MUST include a statement equivalent to: \"{example}\""
            comments = [c for c in feedback if c["type"] == "comment" and c.get("section") == ref]
            items = [f for f, _ in cluster]
            urgent = len(cluster) >= 3 or any(_URGENT.search(c.get("text", "")) for c in comments)
            recs.append(Recommendation(
                id=_rec_id(field_path, after), section=ref, section_title=sec.title,
                priority="critical" if len(cluster) >= 4 else "high" if urgent else "medium",
                title=f"Require recurring content in '{sec.title}' writing instructions",
                summary=(f"Writers added the same content to this section in {len(cluster)} reports: "
                         f"\"{example}\" Make it a standing requirement."),
                confidence=min(0.97, 0.62 + 0.1 * len(cluster)),
                pattern_basis=[i["id"] for i in items] + [c["id"] for c in comments],
                pattern_tags=["writing instructions", "structure"],
                thread=[_thread_item(i) for i in comments[:2] + items[:2]],
                field=field_path, label=f"Writing Instructions — {sec.title}",
                before=sec.instructions, after=after))
        return recs

    # ── paragraph limits ──
    def _paragraph_limit(self, config: ReportConfig, feedback: list[dict[str, Any]]) -> list[Recommendation]:
        by_section: dict[str, list[dict[str, Any]]] = {}
        for fb in feedback:
            t = fb.get("text", "")
            if fb["type"] == "comment" and _PARA_LIMIT.search(t) and _TOO_TIGHT.search(t):
                by_section.setdefault(fb.get("section", ""), []).append(fb)
        recs = []
        for ref, items in by_section.items():
            sec = config.section(ref)
            if len(items) < self.min_occurrences or not sec:
                continue
            current = sec.style.max_paragraphs or 2
            field_path = f"sections[{ref}].styleTone.maxParagraphs"
            recs.append(Recommendation(
                id=_rec_id(field_path, str(current + 1)), section=ref, section_title=sec.title,
                priority="medium",
                title=f"Increase maxParagraphs from {current} to {current + 1} for '{sec.title}'",
                summary=(f"{len(items)} reviewer comments say the {current}-paragraph limit is too tight. "
                         "Raising it stops QA flagging acceptable drafts."),
                confidence=min(0.95, 0.65 + 0.1 * len(items)),
                pattern_basis=[i["id"] for i in items],
                pattern_tags=["style & tone", "paragraph limit", "QA config"],
                thread=[_thread_item(i) for i in items[:3]],
                field=field_path, label=f"Max Paragraphs — {sec.title}",
                before=str(current), after=str(current + 1)))
        return recs


_PATH = re.compile(r"^sections\[(?P<ref>[^\]]+)\]\.(?P<attr>.+)$")


def apply_recommendation(raw_config: dict[str, Any], rec: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of the raw configuration with the recommendation's change applied.

    The version is bumped and the status reset to Draft so the change goes through review.
    """
    change = rec["aiChange"]
    m = _PATH.match(change["field"])
    if not m:
        raise ValueError(f"Unsupported change path: {change['field']}")
    cfg = copy.deepcopy(raw_config)
    ref = m.group("ref")
    sec = next((s for s in cfg.get("sections", []) if s.get("id") == ref or str(s.get("num")) == ref), None)
    if sec is None:
        raise ValueError(f"Section '{ref}' not found.")
    attr, after = m.group("attr"), change["after"]
    if attr == "styleTone.preferredTerminology":
        sec.setdefault("styleTone", {})["preferredTerminology"] = json.loads(after)
    elif attr == "styleTone.maxParagraphs":
        sec.setdefault("styleTone", {})["maxParagraphs"] = int(after)
    elif attr == "writingInstructions":
        sec["instructions"] = after
    elif attr == "contentShape":
        sec["shape"] = after
    else:
        raise ValueError(f"Unsupported change path: {change['field']}")
    cfg["version"] = int(cfg.get("version", 1)) + 1
    cfg["status"] = "Draft"
    return cfg
