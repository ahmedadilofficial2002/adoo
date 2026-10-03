"""Background internet reachability monitor.

VB needs to know whether the outside world is reachable: the AI may be local but
SMS/email/GPS lookups are not. This polls a small set of endpoints with stdlib
only (no extra dependency) and publishes online/offline transitions on the bus.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_ENDPOINTS = ("https://dns.google/generate_204", "https://www.gstatic.com/generate_204")


class InternetMonitor:
    def __init__(
        self,
        endpoints: tuple[str, ...] | list[str] = DEFAULT_ENDPOINTS,
        *,
        interval: float = 30.0,
        timeout: float = 4.0,
        bus: Any = None,
        health: Any = None,
    ) -> None:
        self.endpoints = tuple(endpoints) or DEFAULT_ENDPOINTS
        self.interval = max(5.0, float(interval))
        self.timeout = max(1.0, float(timeout))
        self._bus = bus
        self._health = health
        self._log = logging.getLogger("vb.internet")
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._online: bool | None = None
        self._detail = "not checked yet"
        self._checked_at = 0.0
        self._failures = 0
        self._successes = 0

    # ------------------------------------------------------------------ API
    @property
    def online(self) -> bool | None:
        with self._lock:
            return self._online

    @property
    def detail(self) -> str:
        with self._lock:
            return self._detail

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="vb-internet", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.timeout + 1)
            self._thread = None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "online": self._online,
                "detail": self._detail,
                "checked_at": self._checked_at,
                "checked_text": time.strftime("%H:%M:%S", time.localtime(self._checked_at))
                if self._checked_at
                else "",
                "successes": self._successes,
                "failures": self._failures,
                "interval": self.interval,
            }

    # -------------------------------------------------------------- probing
    def check_once(self) -> bool:
        """Probe every endpoint; the first success wins."""

        online, detail = False, "no endpoints configured"
        for endpoint in self.endpoints:
            ok, why = self._probe(endpoint)
            if ok:
                online, detail = True, endpoint
                break
            detail = why
        with self._lock:
            changed = self._online != online
            previous = self._online
            self._online = online
            self._detail = detail
            self._checked_at = time.time()
            if online:
                self._successes += 1
            else:
                self._failures += 1
        if changed:
            if self._health is not None:
                from vb.core.health import Component, ComponentStatus

                self._health.set(
                    Component.INTERNET,
                    ComponentStatus.ONLINE if online else ComponentStatus.OFFLINE,
                    detail=detail,
                    error="" if online else detail,
                )
            if self._bus is not None:
                self._bus.publish(
                    "internet.changed",
                    source="internet",
                    online=online,
                    detail=detail,
                    previous=None if previous is None else bool(previous),
                )
            (self._log.info if online else self._log.warning)(
                "internet %s (%s)", "online" if online else "offline", detail
            )
        return online

    def _probe(self, endpoint: str) -> tuple[bool, str]:
        try:
            request = urllib.request.Request(endpoint, method="GET")
            request.add_header("User-Agent", "vb-availability-probe")
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if 200 <= int(response.status) < 500:
                    return True, endpoint
                return False, f"{endpoint} -> HTTP {response.status}"
        except urllib.error.HTTPError as exc:  # a 4xx still proves routing works
            return True, f"{endpoint} -> HTTP {exc.code}"
        except urllib.error.URLError as exc:
            return False, f"{endpoint} -> {exc.reason}"
        except (TimeoutError, socket.timeout):
            return False, f"{endpoint} -> timeout after {self.timeout:.0f}s"
        except Exception as exc:
            return False, f"{endpoint} -> {type(exc).__name__}: {exc}"

    def _run(self) -> None:
        self._log.debug("internet monitor started (%.0fs interval)", self.interval)
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception as exc:  # the monitor itself must never die
                self._log.warning("internet probe loop error: %s", exc)
            self._stop.wait(self.interval)
