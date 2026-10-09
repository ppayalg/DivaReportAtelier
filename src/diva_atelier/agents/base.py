"""Shared plumbing for agents: a thread-safe event log every agent writes to."""
from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class Event:
    ts: str
    agent: str
    kind: str
    section: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventLog:
    """Append-only trace of what each agent did. Safe to share across worker threads."""

    def __init__(self) -> None:
        self._events: list[Event] = []
        self._lock = threading.Lock()

    def emit(self, agent: str, kind: str, section: str | None = None, **data: Any) -> None:
        ev = Event(datetime.now(timezone.utc).isoformat(timespec="milliseconds"), agent, kind, section, data)
        with self._lock:
            self._events.append(ev)

    def events(self) -> list[Event]:
        with self._lock:
            return list(self._events)


class Agent:
    """Base class. Subclasses set ``name`` and use ``self.log.emit`` for traceability."""

    name = "agent"

    def __init__(self, log: EventLog | None = None) -> None:
        self.log = log or EventLog()
