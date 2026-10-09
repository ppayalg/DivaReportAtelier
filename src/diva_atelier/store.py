"""File-backed JSON store for configurations, reports and feedback.

Layout under ``root``:  configs/<key>.json   reports/<id>.json   feedback/<key>.json
Writes are atomic (temp file + rename) so a crash never leaves a half-written file.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any

_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class StoreError(ValueError):
    pass


def check_key(key: str) -> str:
    """Keys become file names, so only a conservative character set is allowed."""
    if not _KEY.match(key or ""):
        raise StoreError(f"Invalid key '{key}'. Use letters, digits, '-' and '_' (max 64).")
    return key


class JsonStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._lock = threading.RLock()
        for d in ("configs", "reports", "feedback"):
            (self.root / d).mkdir(parents=True, exist_ok=True)

    def _path(self, kind: str, key: str) -> Path:
        return self.root / kind / f"{check_key(key)}.json"

    def _write(self, path: Path, data: Any) -> None:
        with self._lock:
            fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=2, ensure_ascii=False)
                    fh.write("\n")
                os.replace(tmp, path)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise

    def _read(self, path: Path) -> Any | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    # configs
    def list_configs(self) -> dict[str, dict[str, Any]]:
        return {p.stem: json.loads(p.read_text(encoding="utf-8"))
                for p in sorted((self.root / "configs").glob("*.json"))}

    def get_config(self, key: str) -> dict[str, Any] | None:
        return self._read(self._path("configs", key))

    def save_config(self, key: str, config: dict[str, Any]) -> None:
        self._write(self._path("configs", key), config)

    # reports
    def save_report(self, report: dict[str, Any]) -> None:
        self._write(self._path("reports", report["id"]), report)

    def get_report(self, report_id: str) -> dict[str, Any] | None:
        return self._read(self._path("reports", report_id))

    def list_reports(self) -> list[dict[str, Any]]:
        rows = []
        for p in (self.root / "reports").glob("*.json"):
            r = json.loads(p.read_text(encoding="utf-8"))
            rows.append({k: r.get(k) for k in ("id", "config_key", "config_name", "created_at", "status")}
                        | {"workflow_status": (r.get("workflow") or {}).get("status")})
        return sorted(rows, key=lambda r: r["created_at"] or "", reverse=True)

    # feedback
    def list_feedback(self, key: str) -> list[dict[str, Any]]:
        return self._read(self._path("feedback", key)) or []

    def add_feedback(self, key: str, item: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            items = self.list_feedback(key)
            item = dict(item)
            item.setdefault("id", f"{item.get('type', 'fb')}-{len(items) + 1}")
            items.append(item)
            self._write(self._path("feedback", key), items)
        return item
