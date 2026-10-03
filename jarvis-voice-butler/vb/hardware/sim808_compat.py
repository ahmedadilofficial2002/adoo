"""Value objects kept for the tool layer that predates the state rewrite.

The manager publishes dictionaries from :class:`Sim808State`, but the AI tools
(:mod:`vb.tools.sms`, :mod:`vb.tools.gps`, :mod:`vb.tools.calls`) speak in
``SmsMessage`` / ``GpsFix`` objects and expect ``Sim808Error`` to be raised when
storage cannot be read. These thin wrappers translate between the two worlds so
the manager itself stays free of legacy shapes.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any

from . import at_parsers as at
from .nmea import to_decimal

NO_FIX_MESSAGE = "GPS FIX NOT AVAILABLE: the module has no position yet."


class Sim808Error(RuntimeError):
    """Raised when a modem operation cannot be attempted or confirmed."""


@dataclass
class GpsFix:
    """A position report. ``valid`` is only True on a real fix from the module."""

    valid: bool = False
    latitude: float | None = None
    longitude: float | None = None
    altitude_m: float | None = None
    speed_kmh: float | None = None
    course_deg: float | None = None
    timestamp: str = ""
    message: str = NO_FIX_MESSAGE
    raw: str = ""


@dataclass
class SmsMessage:
    """One message as stored on the module."""

    index: int | None = None
    status: str = ""
    sender: str = ""
    timestamp: str = ""
    body: str = ""
    received_at: float = 0.0


def parse_cgpsinfo(response: str) -> GpsFix:
    """``+CGPSINFO: lat,N,lon,E,DDMMYY,HHMMSS.s,alt,speed,course`` -> GpsFix.

    Also accepts the ``+CGNSINF`` form used by newer SIM808 firmware, where the
    coordinates are already decimal degrees.
    """

    text = (response or "").strip()
    if not text:
        return GpsFix()
    if text.upper().startswith("+CGNSINF"):
        info = at.parse_cgnsinf(text)
        if not info["valid"] or info["latitude"] is None or info["longitude"] is None:
            return GpsFix(raw=text)
        return GpsFix(valid=True, latitude=info["latitude"], longitude=info["longitude"],
                      altitude_m=info["altitude_m"], speed_kmh=info["speed_kmh"],
                      message="", raw=text)
    payload = at.last_value(text, "+CGPSINFO")
    if not payload:
        return GpsFix(raw=text)
    parts = [p.strip() for p in payload.split(",")]
    if len(parts) < 6:
        return GpsFix(raw=text)
    latitude = to_decimal(parts[0], parts[1])
    longitude = to_decimal(parts[2], parts[3])
    if latitude is None or longitude is None:
        return GpsFix(raw=text)
    date, clock = parts[4], parts[5]
    stamp = ""
    if re.fullmatch(r"\d{6}", date):
        stamp = f"20{date[4:6]}-{date[2:4]}-{date[0:2]}"
    if re.fullmatch(r"\d{6}(\.\d+)?", clock):
        stamp = (stamp + " " if stamp else "") + (
            f"{clock[0:2]}:{clock[2:4]}:{clock[4:6]}")
    return GpsFix(valid=True, latitude=latitude, longitude=longitude,
                  altitude_m=_float(parts[6]) if len(parts) > 6 else None,
                  speed_kmh=_float(parts[7]) if len(parts) > 7 else None,
                  course_deg=_float(parts[8]) if len(parts) > 8 else None,
                  timestamp=stamp, message="", raw=text)


def parse_cmgl(response: str) -> list[SmsMessage]:
    """AT+CMGL response -> list of :class:`SmsMessage`."""

    return [to_message(item) for item in at.parse_cmgl(response)]


def to_message(item: dict[str, Any]) -> SmsMessage:
    """Convert the manager's message dict into the tool-facing dataclass."""

    return SmsMessage(index=item.get("index"),
                      status=str(item.get("status") or ""),
                      sender=str(item.get("from") or item.get("sender") or ""),
                      timestamp=str(item.get("timestamp") or ""),
                      body=str(item.get("text") or item.get("body") or ""),
                      received_at=float(item.get("received_at") or 0.0))


def to_fix(state: Any) -> GpsFix:
    """Build a GpsFix straight from a :class:`Sim808State` (no fresh command)."""

    if not getattr(state, "gps_valid", False):
        message = getattr(state, "gps_message", "") or NO_FIX_MESSAGE
        if getattr(state, "gps_enabled", False) and not message:
            message = "GPS IS ON BUT HAS NO FIX YET."
        if not getattr(state, "gps_enabled", False):
            message = "GPS IS DISABLED: ask for gps_start first. " + NO_FIX_MESSAGE
        return GpsFix(valid=False, message=message)
    return GpsFix(valid=True,
                  latitude=getattr(state, "gps_latitude", None),
                  longitude=getattr(state, "gps_longitude", None),
                  altitude_m=getattr(state, "gps_altitude_m", None),
                  speed_kmh=getattr(state, "gps_speed_kmh", None),
                  timestamp=time_string(getattr(state, "gps_fixed_at", 0.0)),
                  message="",
                  raw="+CGNSINF (cached state)")


def time_string(epoch: float) -> str:
    """Render an epoch as ``YYYY-MM-DD HH:MM:SS`` ("" when unset)."""

    if not epoch:
        return ""
    return dt.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def _float(raw: str) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
