"""A tiny thread-safe publish/subscribe bus.

Every subsystem (SIM808 monitor, dashboard, desktop UI, AI runtime) talks through
this bus instead of importing each other. That keeps the AI alive when the UI or
Flask is broken: a subscriber that raises is dropped and reported, never allowed
to propagate back into the publisher.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class Event:
    """One immutable fact about what just happened."""

    topic: str
    source: str
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "source": self.source,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "time_text": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.timestamp)),
        }


Listener = Callable[[Event], None]


class EventBus:
    """Listeners are per-topic callables. Publisher never sees listener errors."""

    def __init__(self, history_size: int = 250) -> None:
        self._lock = threading.RLock()
        self._listeners: dict[str, list[Listener]] = {}
        self._recent: deque[Event] = deque(maxlen=max(10, int(history_size)))
        self._log = logging.getLogger("vb.core.bus")
        self._dropped = 0

    def subscribe(self, topic: str, listener: Listener) -> Callable[[], None]:
        """Register *listener* for *topic*. Returns an unsubscribe callable.

        Use the topic "*" to receive every event.
        """

        if not callable(listener):
            raise TypeError("listener must be callable")
        with self._lock:
            self._listeners.setdefault(topic, [])
            if listener not in self._listeners[topic]:
                self._listeners[topic].append(listener)

        def unsubscribe() -> None:
            with self._lock:
                bucket = self._listeners.get(topic, [])
                if listener in bucket:
                    bucket.remove(listener)

        return unsubscribe

    def publish(self, topic: str, source: str = "core", **payload: Any) -> Event:
        event = Event(topic=topic, source=source, payload=payload)
        with self._lock:
            self._recent.append(event)
            targets = list(self._listeners.get(topic, [])) + list(self._listeners.get("*", []))
        for listener in targets:
            try:
                listener(event)
            except Exception as exc:  # fault isolation: never kill the publisher
                self._dropped += 1
                self._log.warning("subscriber %r failed on %s: %s", listener, topic, exc)
        return event

    def recent(self, topic_prefix: str = "", limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            events = list(self._recent)
        if topic_prefix:
            events = [e for e in events if e.topic.startswith(topic_prefix)]
        return [e.to_dict() for e in events[-int(limit):]]

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return sum(len(v) for v in self._listeners.values())

    @property
    def failed_subscribers(self) -> int:
        return self._dropped

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "topics": {k: len(v) for k, v in self._listeners.items() if v},
                "history": len(self._recent),
                "failed_subscribers": self._dropped,
            }
