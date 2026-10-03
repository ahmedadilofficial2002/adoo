"""Startup + runtime health of every subsystem.

The rule this enforces: a subsystem that is disabled or broken must be recorded
with a reason, never left ambiguous, and never fatal to the rest of VB.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Component(str, Enum):
    AI = "AI_ASSISTANT"
    VOICE = "VOICE"
    DESKTOP_UI = "DESKTOP_UI"
    DASHBOARD = "FLASK_DASHBOARD"
    SIM808 = "SIM808"
    INTERNET = "INTERNET"
    HISTORY = "HISTORY"
    CAMERA = "CAMERA"
    EMAIL = "EMAIL"


class ComponentStatus(str, Enum):
    STARTING = "STARTING"
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    DEGRADED = "DEGRADED"
    RECOVERING = "RECOVERING"
    DISABLED = "DISABLED"


#: Statuses that still count as "the app is usable".
WORKING = {ComponentStatus.ONLINE, ComponentStatus.DEGRADED, ComponentStatus.RECOVERING}


@dataclass
class ComponentHealth:
    component: str
    status: ComponentStatus = ComponentStatus.STARTING
    detail: str = ""
    error: str = ""
    enabled_by_config: bool = True
    changed_at: float = field(default_factory=time.time)
    started_at: float = field(default_factory=time.time)
    restart_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "status": self.status.value,
            "detail": self.detail,
            "error": self.error,
            "enabled_by_config": self.enabled_by_config,
            "working": self.status in WORKING,
            "changed_at": self.changed_at,
            "started_at": self.started_at,
            "restart_count": self.restart_count,
            "uptime_seconds": round(time.time() - self.started_at, 1)
            if self.status in WORKING
            else 0,
        }


class HealthRegistry:
    """Thread-safe registry of component health."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._items: dict[str, ComponentHealth] = {}
        self._log = logging.getLogger("vb.core.health")

    def declared(self, component: Component | str, enabled: bool = True) -> ComponentHealth:
        """Record that a subsystem exists and whether config asked for it."""

        name = component.value if isinstance(component, Component) else str(component)
        with self._lock:
            item = self._items.get(name)
            if item is None:
                item = ComponentHealth(component=name, enabled_by_config=enabled)
                if not enabled:
                    item.status = ComponentStatus.DISABLED
                    item.detail = "disabled in settings"
                self._items[name] = item
            else:
                item.enabled_by_config = enabled
            return item

    def set(
        self,
        component: Component | str,
        status: ComponentStatus,
        detail: str = "",
        error: str = "",
    ) -> ComponentHealth:
        name = component.value if isinstance(component, Component) else str(component)
        with self._lock:
            item = self._items.setdefault(name, ComponentHealth(component=name))
            if status is ComponentStatus.RECOVERING and item.status is not ComponentStatus.RECOVERING:
                item.restart_count += 1
            item.status = status
            item.detail = detail
            item.error = error
            item.changed_at = time.time()
            if status in WORKING and item.started_at == 0:
                item.started_at = time.time()
            return item

    def disabled(self, component: Component | str, reason: str) -> ComponentHealth:
        return self.set(component, ComponentStatus.DISABLED, detail=reason)

    def get(self, component: Component | str) -> ComponentHealth | None:
        name = component.value if isinstance(component, Component) else str(component)
        with self._lock:
            return self._items.get(name)

    def is_working(self, component: Component | str) -> bool:
        item = self.get(component)
        return bool(item and item.status in WORKING)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {name: item.to_dict() for name, item in sorted(self._items.items())}

    def summary(self) -> dict[str, Any]:
        items = list(self.snapshot().values())
        online = [i["component"] for i in items if i["status"] == ComponentStatus.ONLINE.value]
        degraded = [i["component"] for i in items if i["status"] == ComponentStatus.DEGRADED.value]
        recovering = [i["component"] for i in items if i["status"] == ComponentStatus.RECOVERING.value]
        offline = [i["component"] for i in items if i["status"] == ComponentStatus.OFFLINE.value]
        disabled = [i["component"] for i in items if i["status"] == ComponentStatus.DISABLED.value]
        return {
            "online": online,
            "degraded": degraded,
            "recovering": recovering,
            "offline": offline,
            "disabled": disabled,
            "ready": bool(online or degraded or recovering),
        }

    def startup_report(self) -> str:
        """Human-readable line per subsystem, printed once at boot."""

        lines = []
        for name, item in self.snapshot().items():
            mark = {
                ComponentStatus.ONLINE.value: "ON ",
                ComponentStatus.DEGRADED.value: "WARN",
                ComponentStatus.RECOVERING.value: "RECO",
                ComponentStatus.STARTING.value: "....",
                ComponentStatus.DISABLED.value: "OFF",
                ComponentStatus.OFFLINE.value: "FAIL",
            }.get(item["status"], "??")
            note = item["detail"] or item["error"]
            lines.append(f"  [{mark}] {name:<16} {note}")
        return "\n".join(lines)
