"""Structured error monitoring.

Every subsystem reports failures here instead of printing to stdout, so the
dashboard, the desktop UI and the log file all show the same thing:
which component broke, how bad it is, and whether VB is recovering.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


_LOG_LEVELS = {
    Severity.INFO: logging.INFO,
    Severity.WARNING: logging.WARNING,
    Severity.ERROR: logging.ERROR,
    Severity.CRITICAL: logging.CRITICAL,
}


def _error_type(exc_or_text: Any) -> str:
    if isinstance(exc_or_text, BaseException):
        return type(exc_or_text).__name__
    return ""


@dataclass
class ErrorRecord:
    component: str
    message: str
    error_type: str = ""
    severity: Severity = Severity.ERROR
    recovery: str = "none"
    timestamp: float = field(default_factory=time.time)
    context: dict[str, Any] = field(default_factory=dict)
    repeat_count: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "error_type": self.error_type,
            "message": self.message,
            "severity": self.severity.value,
            "recovery": self.recovery,
            "timestamp": self.timestamp,
            "time_text": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.timestamp)),
            "context": self.context,
            "repeat_count": self.repeat_count,
        }


class ErrorMonitor:
    """Collects errors with a bounded in-memory ring plus optional persistence."""

    def __init__(self, bus: Any = None, history: Any = None, capacity: int = 200) -> None:
        self._lock = threading.RLock()
        self._records: deque[ErrorRecord] = deque(maxlen=max(10, int(capacity)))
        self._bus = bus
        self._history = history
        self._log = logging.getLogger("vb.core.errors")

    def attach(self, bus: Any = None, history: Any = None) -> None:
        """Wire the bus/history once they exist (they may start after we do)."""

        with self._lock:
            if bus is not None:
                self._bus = bus
            if history is not None:
                self._history = history

    def record(
        self,
        component: ComponentOrStr,
        error: Any,
        severity: Severity = Severity.ERROR,
        recovery: str = "none",
        context: dict[str, Any] | None = None,
        publish: bool = True,
    ) -> ErrorRecord:
        """Log, store and broadcast one failure. Never raises."""

        name = getattr(component, "value", str(component))
        if isinstance(error, BaseException):
            message, error_type = str(error) or type(error).__name__, type(error).__name__
        else:
            message, error_type = str(error), ""
        record = ErrorRecord(
            component=name,
            message=message[:500],
            error_type=error_type,
            severity=severity,
            recovery=recovery,
            context=dict(context or {}),
        )
        with self._lock:
            previous = self._records[-1] if self._records else None
            if (
                previous
                and previous.component == name
                and previous.message == record.message
                and time.time() - previous.timestamp < 60
            ):
                previous.repeat_count += 1
                previous.timestamp = record.timestamp
                record = previous
            else:
                self._records.append(record)
            bus, history = self._bus, self._history

        logging.getLogger(f"vb.{name.lower()}").log(_LOG_LEVELS[record.severity], "%s: %s", name, record.message)
        if history is not None:
            try:
                history.add_error(record)
            except Exception as exc:  # persistence must never mask the original fault
                self._log.warning("history could not store error: %s", exc)
        if publish and bus is not None:
            try:
                bus.publish("error", source=name, **record.to_dict())
            except Exception as exc:
                self._log.warning("bus could not publish error: %s", exc)
        return record

    def recent(self, component: str = "", limit: int = 20, severity: str = "") -> list[dict[str, Any]]:
        with self._lock:
            records = list(self._records)
        if component:
            records = [r for r in records if r.component == component]
        if severity:
            records = [r for r in records if r.severity.value == severity]
        return [r.to_dict() for r in records[-int(limit):]]

    def counts(self) -> dict[str, int]:
        with self._lock:
            out = {s.value: 0 for s in Severity}
            for record in self._records:
                out[record.severity.value] += 1
            return out

    def latest(self) -> dict[str, Any] | None:
        with self._lock:
            return self._records[-1].to_dict() if self._records else None


ComponentOrStr = Any
