"""Dependency-free retriever for local use, demos and tests.

Keyword mode scores the share of query terms found in a document. Semantic mode blends
idf-weighted query coverage with TF-IDF cosine similarity, a lexical approximation of embedding
search (rare terms count for more). Hybrid averages Keyword and Semantic. For production
semantic search implement the ``Retriever`` protocol on top of your vector database.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path

from .base import Document, Hit

_STOP = frozenset("""a an and are as at be by for from has have in is it its of on or that the this to was were
with we our their into over per not no than then so if but also any all each""".split())
_TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    out = []
    for t in _TOKEN.findall((text or "").lower()):
        if t in _STOP:
            continue
        if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        out.append(t)
    return out


class InMemoryRetriever:
    def __init__(self, documents: list[Document] | None = None):
        self.documents: list[Document] = list(documents or [])

    def add(self, doc: Document) -> None:
        self.documents.append(doc)

    def search(self, *, index: str, query: str, mode: str = "Semantic", top_k: int = 3,
               min_score: float = 0.0, content_class: str = "") -> list[Hit]:
        pool = [d for d in self.documents
                if d.index == index and (not content_class or d.content_class == content_class)]
        if not pool:
            return []
        if mode == "MetadataOnly":
            hits = [Hit(d, 1.0) for d in pool]
            hits.sort(key=lambda h: str(h.doc.metadata.get("date", "")), reverse=True)
            return hits[:top_k]

        q = tokenize(query)
        if not q:
            return []
        docs_tokens = [tokenize(d.text) for d in pool]
        n = len(pool)
        df: Counter[str] = Counter()
        for toks in docs_tokens:
            df.update(set(toks))

        def idf(t: str) -> float:
            return math.log((n + 1) / (df.get(t, 0) + 1)) + 1.0

        def vec(toks: list[str]) -> dict[str, float]:
            c = Counter(toks)
            return {t: (1 + math.log(f)) * idf(t) for t, f in c.items()}

        def cosine(a: dict[str, float], b: dict[str, float]) -> float:
            dot = sum(v * b.get(t, 0.0) for t, v in a.items())
            na = math.sqrt(sum(v * v for v in a.values()))
            nb = math.sqrt(sum(v * v for v in b.values()))
            return dot / (na * nb) if na and nb else 0.0

        qv, qset = vec(q), set(q)
        q_weight = sum(idf(t) for t in qset)
        hits: list[Hit] = []
        for d, toks in zip(pool, docs_tokens):
            dset = set(toks)
            kw = len(qset & dset) / len(qset)
            wcov = sum(idf(t) for t in qset & dset) / q_weight
            sem = 0.7 * wcov + 0.3 * cosine(qv, vec(toks))
            score = kw if mode == "Keyword" else sem if mode == "Semantic" else 0.5 * kw + 0.5 * sem
            if score > 0 and score >= min_score:
                hits.append(Hit(d, round(min(score, 1.0), 4)))
        hits.sort(key=lambda h: (-h.score, h.doc.id))
        return hits[:top_k]


def load_knowledge(directory: str | Path) -> InMemoryRetriever:
    """Load every ``*.jsonl`` file in a directory. The file name is the default index name.

    Each line: {"id": "...", "text": "...", "content_class": "Narrative", "metadata": {...}}
    """
    retriever = InMemoryRetriever()
    for path in sorted(Path(directory).glob("*.jsonl")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            row = json.loads(line)
            retriever.add(Document(
                id=row.get("id", f"{path.stem}-{n}"), index=row.get("index", path.stem),
                text=row["text"], content_class=row.get("content_class", "Narrative"),
                metadata=row.get("metadata", {}),
            ))
    return retriever
