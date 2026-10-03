"""Persistent history for SMS, calls, SIM808 events and errors.

SQLite comes from the standard library, so storage adds no new dependency. The
store is deliberately fail-soft: if the database cannot be opened or written it
flips to an in-memory ring buffer and keeps going. Losing history is acceptable;
crashing the assistant is not.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    direction   TEXT NOT NULL,
    number      TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL DEFAULT '',
    timestamp   REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    reference   TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT 'sim808'
);
CREATE INDEX IF NOT EXISTS idx_sms_ts ON sms (timestamp DESC);
CREATE TABLE IF NOT EXISTS calls (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    number            TEXT NOT NULL DEFAULT '',
    direction         TEXT NOT NULL DEFAULT 'OUTGOING',
    state             TEXT NOT NULL DEFAULT 'DETECTED',
    detected_at       REAL NOT NULL,
    dialed_at         REAL,
    answered_at       REAL,
    ended_at          REAL,
    duration_seconds  REAL,
    end_reason        TEXT NOT NULL DEFAULT '',
    source            TEXT NOT NULL DEFAULT 'sim808'
);
CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls (detected_at DESC);
CREATE TABLE IF NOT EXISTS sim_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    number      TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    timestamp   REAL NOT NULL,
    source      TEXT NOT NULL DEFAULT 'sim808'
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON sim_events (timestamp DESC);
CREATE TABLE IF NOT EXISTS errors (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    component     TEXT NOT NULL,
    error_type    TEXT NOT NULL DEFAULT '',
    message       TEXT NOT NULL DEFAULT '',
    severity      TEXT NOT NULL DEFAULT 'ERROR',
    recovery      TEXT NOT NULL DEFAULT 'none',
    repeat_count  INTEGER NOT NULL DEFAULT 1,
    timestamp     REAL NOT NULL,
    context       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_errors_ts ON errors (timestamp DESC);
"""


@dataclass
class RetentionPolicy:
    """How much history to keep before pruning."""

    max_age_days: float = 30.0
    max_rows: int = 5000
    purge_interval_seconds: float = 900.0

    @classmethod
    def from_settings(cls, section: Any) -> "RetentionPolicy":
        if section is None:
            return cls()
        return cls(
            max_age_days=float(getattr(section, "max_age_days", 30.0)),
            max_rows=int(getattr(section, "max_rows", 5000)),
            purge_interval_seconds=float(getattr(section, "purge_interval_seconds", 900.0)),
        )


@dataclass
class CallRecord:
    """One call, tracked from first indication until it ends."""

    number: str = ""
    direction: str = "OUTGOING"
    state: str = "DETECTED"
    detected_at: float = field(default_factory=time.time)
    dialed_at: float | None = None
    answered_at: float | None = None
    ended_at: float | None = None
    duration_seconds: float | None = None
    end_reason: str = ""
    source: str = "sim808"
    row_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["time_text"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.detected_at))
        data["active"] = self.ended_at is None
        return data


class HistoryStore:
    """Thread-safe SQLite history with an in-memory fallback."""

    TABLES = ("sms", "calls", "sim_events", "errors")
    _TIME_COLUMN = {"calls": "detected_at"}

    def __init__(self, path: Path | str | None = None, retention: RetentionPolicy | None = None) -> None:
        self._lock = threading.RLock()
        self._log = logging.getLogger("vb.history")
        self.retention = retention or RetentionPolicy()
        self.path = Path(path).expanduser() if path else None
        self.available = False
        self.reason = "no database path configured"
        self._conn: sqlite3.Connection | None = None
        self._last_purge = 0.0
        # Memory ring buffers: fallback when SQLite is unavailable, and a cheap
        # cache so the dashboard still has something to show.
        self._memory: dict[str, deque[dict[str, Any]]] = {t: deque(maxlen=500) for t in self.TABLES}
        self._writes = 0
        self._write_failures = 0
        if self.path is not None:
            self._open()

    # ---------------------------------------------------------------- setup
    def _open(self) -> bool:
        assert self.path is not None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5.0)
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(_SCHEMA_VERSION),),
            )
            self._conn.commit()
            self.available = True
            self.reason = f"sqlite at {self.path}"
            self._log.info("history store ready at %s", self.path)
        except (sqlite3.Error, OSError) as exc:
            self.available = False
            self.reason = f"{type(exc).__name__}: {exc}"
            self._conn = None
            self._log.warning("history persistence unavailable (%s); memory only", self.reason)
        return self.available

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.commit()
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None

    # ------------------------------------------------------------- writing
    def _execute(self, sql: str, params: tuple) -> int | None:
        """Run one write. Returns lastrowid, or None on failure (never raises)."""

        with self._lock:
            if self._conn is None:
                return None
            try:
                cursor = self._conn.execute(sql, params)
                self._conn.commit()
                self._writes += 1
                return cursor.lastrowid
            except sqlite3.Error as exc:
                self._write_failures += 1
                self._log.warning("history write failed: %s", exc)
                if "no such table" in str(exc).lower():
                    try:
                        self._conn.executescript(_SCHEMA)
                        self._conn.commit()
                    except sqlite3.Error:
                        self.close()
                return None

    def _query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            if self._conn is None:
                return []
            try:
                return [dict(row) for row in self._conn.execute(sql, params).fetchall()]
            except sqlite3.Error as exc:
                self._log.warning("history query failed: %s", exc)
                return []

    def add_sms(
        self,
        direction: str,
        number: str,
        text: str,
        status: str = "OK",
        error: str = "",
        reference: str = "",
        timestamp: float | None = None,
        source: str = "sim808",
    ) -> dict[str, Any]:
        """Store one inbound or outbound SMS."""

        stamp = float(timestamp if timestamp is not None else time.time())
        record: dict[str, Any] = {
            "direction": str(direction).upper(),
            "number": number or "",
            "text": text or "",
            "timestamp": stamp,
            "status": status or "",
            "error": error or "",
            "reference": reference or "",
            "source": source,
        }
        record["id"] = self._execute(
            "INSERT INTO sms (direction, number, text, timestamp, status, error, reference, source)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (record["direction"], record["number"], record["text"], stamp, record["status"],
             record["error"], record["reference"], source),
        )
        self._memory["sms"].append(record)
        self.maybe_purge()
        return record

    def add_call(self, record: CallRecord) -> CallRecord:
        record.row_id = self._execute(
            "INSERT INTO calls (number, direction, state, detected_at, dialed_at, answered_at,"
            " ended_at, duration_seconds, end_reason, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (record.number, record.direction, record.state, record.detected_at, record.dialed_at,
             record.answered_at, record.ended_at, record.duration_seconds, record.end_reason,
             record.source),
        )
        self._memory["calls"].append(record.to_dict())
        self.maybe_purge()
        return record

    def update_call(self, record: CallRecord) -> CallRecord:
        """Persist a state transition; derives duration from real timestamps."""

        if record.answered_at and not record.duration_seconds:
            end = record.ended_at or time.time()
            record.duration_seconds = max(0.0, round(end - record.answered_at, 1))
        if record.row_id is None:
            return self.add_call(record)
        self._execute(
            "UPDATE calls SET number=?, direction=?, state=?, detected_at=?, dialed_at=?,"
            " answered_at=?, ended_at=?, duration_seconds=?, end_reason=? WHERE id=?",
            (record.number, record.direction, record.state, record.detected_at, record.dialed_at,
             record.answered_at, record.ended_at, record.duration_seconds, record.end_reason,
             record.row_id),
        )
        return record

    def add_event(
        self,
        kind: str,
        name: str = "",
        detail: str = "",
        status: str = "",
        error: str = "",
        number: str = "",
        timestamp: float | None = None,
        source: str = "sim808",
    ) -> dict[str, Any]:
        """Store one SIM808 lifecycle event (SMS, call, GPS, network, SIM, status)."""

        stamp = float(timestamp if timestamp is not None else time.time())
        record: dict[str, Any] = {
            "kind": kind,
            "name": name or "",
            "number": number or "",
            "detail": detail or "",
            "status": status or "",
            "error": error or "",
            "timestamp": stamp,
            "source": source,
        }
        record["id"] = self._execute(
            "INSERT INTO sim_events (kind, name, number, detail, status, error, timestamp, source)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (kind, record["name"], record["number"], record["detail"], record["status"],
             record["error"], stamp, source),
        )
        self._memory["sim_events"].append(record)
        self.maybe_purge()
        return record

    def add_error(self, record: Any) -> dict[str, Any]:
        """Persist an ErrorRecord (or any mapping with the same keys)."""

        data = record.to_dict() if hasattr(record, "to_dict") else dict(record)
        stamp = float(data.get("timestamp") or time.time())
        stored: dict[str, Any] = {
            "component": data.get("component", ""),
            "error_type": data.get("error_type", ""),
            "message": data.get("message", ""),
            "severity": data.get("severity", "ERROR"),
            "recovery": data.get("recovery", "none"),
            "repeat_count": int(data.get("repeat_count", 1) or 1),
            "timestamp": stamp,
            "context": data.get("context", {}),
        }
        stored["id"] = self._execute(
            "INSERT INTO errors (component, error_type, message, severity, recovery, repeat_count,"
            " timestamp, context) VALUES (?,?,?,?,?,?,?,?)",
            (stored["component"], stored["error_type"], stored["message"], stored["severity"],
             stored["recovery"], stored["repeat_count"], stamp,
             json.dumps(stored["context"], default=str)[:2000]),
        )
        self._memory["errors"].append(stored)
        self.maybe_purge()
        return stored

    # -------------------------------------------------------------- reading
    def _rows_or_memory(
        self, sql: str, params: tuple, table: str, limit: int, time_key: str
    ) -> list[dict[str, Any]]:
        rows = self._query(sql, params)
        if rows:
            for row in rows:
                row["time_text"] = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(float(row.get(time_key, 0)))
                )
            return rows
        cached = [dict(item) for item in self._memory[table]]
        cached.sort(key=lambda item: float(item.get(time_key, 0)), reverse=True)
        for item in cached:
            item["time_text"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(float(item.get(time_key, 0)))
            )
        return cached[:limit]

    def list_sms(self, limit: int = 20, direction: str = "") -> list[dict[str, Any]]:
        where, params = "", []
        if direction:
            where, params = "WHERE direction=?", [str(direction).upper()]
        return self._rows_or_memory(
            f"SELECT * FROM sms {where} ORDER BY timestamp DESC LIMIT ?",
            (*params, int(limit)), "sms", int(limit), "timestamp",
        )

    def list_calls(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._rows_or_memory(
            "SELECT * FROM calls ORDER BY detected_at DESC LIMIT ?",
            (int(limit),), "calls", int(limit), "detected_at",
        )

    def list_events(self, limit: int = 20, kind: str = "") -> list[dict[str, Any]]:
        where, params = "", []
        if kind:
            where, params = "WHERE kind=?", [kind]
        return self._rows_or_memory(
            f"SELECT * FROM sim_events {where} ORDER BY timestamp DESC LIMIT ?",
            (*params, int(limit)), "sim_events", int(limit), "timestamp",
        )

    def list_errors(self, limit: int = 20, component: str = "") -> list[dict[str, Any]]:
        where, params = "", []
        if component:
            where, params = "WHERE component=?", [component]
        return self._rows_or_memory(
            f"SELECT * FROM errors {where} ORDER BY timestamp DESC LIMIT ?",
            (*params, int(limit)), "errors", int(limit), "timestamp",
        )

    def stats(self) -> dict[str, Any]:
        counts: dict[str, Any] = {}
        for table, time_key in (("sms", "timestamp"), ("calls", "detected_at"),
                                ("sim_events", "timestamp"), ("errors", "timestamp")):
            rows = self._query(f"SELECT COUNT(*) AS n FROM {table}")
            counts[table] = int(rows[0]["n"]) if rows else len(self._memory[table])
        counts.update({
            "available": self.available,
            "reason": self.reason,
            "path": str(self.path) if self.path else "",
            "writes": self._writes,
            "write_failures": self._write_failures,
            "retention_days": self.retention.max_age_days,
            "retention_rows": self.retention.max_rows,
        })
        return counts

    # ------------------------------------------------------------- retention
    def maybe_purge(self) -> int:
        """Prune at most once per purge interval so writes stay cheap."""

        now = time.time()
        if now - self._last_purge < self.retention.purge_interval_seconds:
            return 0
        self._last_purge = now
        return self.purge()

    def purge(self) -> int:
        """Delete rows older than max_age_days and any overflow past max_rows."""

        if self._conn is None:
            return 0
        cutoff = time.time() - max(0.001, float(self.retention.max_age_days)) * 86400
        removed = 0
        for table in self.TABLES:
            time_key = self._TIME_COLUMN.get(table, "timestamp")
            removed += self._delete(f"DELETE FROM {table} WHERE {time_key} < ?", (cutoff,))
            overflow = self._query(
                f"SELECT id FROM {table} ORDER BY {time_key} DESC LIMIT -1 OFFSET ?",
                (int(self.retention.max_rows),),
            )
            if overflow:
                ids = tuple(row["id"] for row in overflow)
                removed += self._delete(
                    f"DELETE FROM {table} WHERE id IN ({','.join('?' * len(ids))})", ids
                )
        if removed:
            self._log.info("history retention pruned %d row(s)", removed)
        return removed

    def _delete(self, sql: str, params: tuple) -> int:
        with self._lock:
            if self._conn is None:
                return 0
            try:
                cursor = self._conn.execute(sql, params)
                self._conn.commit()
                return int(cursor.rowcount or 0)
            except sqlite3.Error:
                return 0

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<HistoryStore available={self.available} path={self.path}>"
