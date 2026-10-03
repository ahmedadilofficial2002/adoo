"""Centralised SIM808 status object.

This is the single source of truth for modem state. The manager owns and mutates
it; the Flask dashboard and the AI tools only ever read a snapshot. That is what
keeps the rule "the dashboard must never open the serial port" true: there is
nothing to open, because the latest known state is already published here.

Honesty rule: `online` means the module answered `AT` with `OK`. A merely open
serial port is tracked separately as `serial_open` and never reported as online.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

UNKNOWN = "UNKNOWN"


@dataclass
class Sim808State:
    """Everything the dashboard and the AI are allowed to know about the modem."""

    # ---- link / ownership -------------------------------------------------
    port: str = ""
    baudrate: int = 0
    serial_open: bool = False          # OS-level: the COM port could be opened
    module_responding: bool = False    # AT handshake answered OK (== truly online)
    online: bool = False               # derived: serial_open AND module_responding
    owner: str = "sim808_manager"      # who holds the port (always the manager)

    # ---- module identity --------------------------------------------------
    module_model: str = ""
    firmware: str = ""
    imei: str = ""

    # ---- SIM --------------------------------------------------------------
    sim_status: str = UNKNOWN          # READY / PIN REQUIRED / NOT INSERTED / ...
    sim_ready: bool = False
    imsi: str = ""
    iccid: str = ""
    msisdn: str = ""
    operator: str = ""

    # ---- radio / network --------------------------------------------------
    signal_rssi: int = 99              # 0-31, 99 = no signal / not detectable
    signal_percent: int = 0
    bit_error_rate: str = "99"
    registration_csd: str = UNKNOWN    # AT+CREG?  -> 0..5
    registration_gprs: str = UNKNOWN   # AT+CGREG? -> 0..5
    registered: bool = False
    radio_disabled: bool | None = None
    gprs_attached: bool = False

    # ---- power ------------------------------------------------------------
    battery_percent: int | None = None
    battery_volts: float | None = None
    charging: bool | None = None

    # ---- SMS --------------------------------------------------------------
    sms_storage: str = ""
    sms_format: str = ""
    unread_sms: int = 0
    last_sms_from: str = ""
    last_sms_at: float = 0.0
    last_sms_send_ok: bool | None = None
    last_sms_send_at: float = 0.0
    last_sms_error: str = ""

    # ---- call -------------------------------------------------------------
    call_state: str = "IDLE"           # IDLE/OUTGOING/RINGING/ACTIVE/ENDED
    call_number: str = ""
    call_direction: str = ""
    call_since: float = 0.0
    call_duration_seconds: float = 0.0
    last_call_number: str = ""
    last_call_at: float = 0.0
    last_call_duration: float = 0.0
    last_call_reason: str = ""

    # ---- GPS --------------------------------------------------------------
    gps_enabled: bool = False
    gps_valid: bool = False
    gps_latitude: float | None = None
    gps_longitude: float | None = None
    gps_altitude_m: float | None = None
    gps_speed_kmh: float | None = None
    gps_fixed_at: float = 0.0
    gps_message: str = ""

    # ---- AT activity / errors --------------------------------------------
    last_at_command: str = ""
    last_at_command_at: float = 0.0
    last_at_response: str = ""
    last_at_response_at: float = 0.0
    last_error: str = ""
    last_error_at: float = 0.0
    error_count: int = 0
    at_commands_sent: int = 0
    at_timeouts: int = 0
    reconnects: int = 0

    updated_at: float = field(default_factory=time.time)

    # ---------------------------------------------------------------- mutate
    def touch(self) -> None:
        self.updated_at = time.time()

    def note_error(self, message: str) -> None:
        self.last_error = str(message)[:300]
        self.last_error_at = time.time()
        self.error_count += 1
        self.touch()

    def note_at(self, command: str, response: str = "") -> None:
        self.last_at_command = command[:120]
        self.last_at_command_at = time.time()
        if response:
            self.last_at_response = response[:300]
            self.last_at_response_at = time.time()
        self.at_commands_sent += 1
        self.touch()

    def mark_offline(self, reason: str = "") -> None:
        self.serial_open = False
        self.module_responding = False
        self.online = False
        self.registered = False
        self.gprs_attached = False
        self.sim_ready = False
        self.sim_status = UNKNOWN
        if reason:
            self.note_error(reason)
        self.touch()

    # --------------------------------------------------------------- derived
    def signal_label(self) -> str:
        if self.signal_rssi in (99, 0) and not self.signal_percent:
            return "no signal"
        return f"{self.signal_percent}% ({self.signal_rssi}/31)"

    def status_line(self) -> str:
        """One honest sentence describing the modem, for the AI to speak."""

        if not self.serial_open:
            return f"SIM808 serial port {self.port or 'not configured'} is closed."
        if not self.module_responding:
            return f"SIM808 port {self.port} is open but the module is not answering AT."
        bits = [f"online on {self.operator or self.registration_csd}"]
        bits.append(f"signal {self.signal_label()}")
        if self.sim_status != UNKNOWN:
            bits.append(f"SIM {self.sim_status.lower()}")
        if self.call_state not in ("", "IDLE", "ENDED"):
            bits.append(f"call {self.call_state.lower()} {self.call_number}".strip())
        bits.append(f"{self.unread_sms} unread SMS")
        bits.append("GPS fix" if self.gps_valid else "no GPS fix")
        return "SIM808 " + ", ".join(bits) + "."

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe snapshot for the dashboard and event payloads."""

        return {
            "online": self.online,
            "serial_open": self.serial_open,
            "module_responding": self.module_responding,
            "owner": self.owner,
            "port": self.port,
            "baudrate": self.baudrate,
            "module": {
                "model": self.module_model,
                "firmware": self.firmware,
                "imei": self.imei,
            },
            "sim": {
                "status": self.sim_status,
                "ready": self.sim_ready,
                "imsi": self.imsi,
                "iccid": self.iccid,
                "msisdn": self.msisdn,
                "operator": self.operator,
            },
            "network": {
                "registered": self.registered,
                "registration_csd": self.registration_csd,
                "registration_gprs": self.registration_gprs,
                "signal_rssi": self.signal_rssi,
                "signal_percent": self.signal_percent,
                "signal_label": self.signal_label(),
                "bit_error_rate": self.bit_error_rate,
                "radio_disabled": self.radio_disabled,
                "gprs_attached": self.gprs_attached,
            },
            "power": {
                "battery_percent": self.battery_percent,
                "battery_volts": self.battery_volts,
                "charging": self.charging,
            },
            "sms": {
                "storage": self.sms_storage,
                "format": self.sms_format,
                "unread": self.unread_sms,
                "last_from": self.last_sms_from,
                "last_sms_at": self.last_sms_at,
                "last_send_ok": self.last_sms_send_ok,
                "last_send_at": self.last_sms_send_at,
                "last_error": self.last_sms_error,
            },
            "call": {
                "state": self.call_state,
                "number": self.call_number,
                "direction": self.call_direction,
                "since": self.call_since,
                "duration_seconds": self.call_duration_seconds,
                "last_number": self.last_call_number,
                "last_call_at": self.last_call_at,
                "last_call_duration": self.last_call_duration,
                "last_reason": self.last_call_reason,
            },
            "gps": {
                "enabled": self.gps_enabled,
                "valid": self.gps_valid,
                "latitude": self.gps_latitude,
                "longitude": self.gps_longitude,
                "altitude_m": self.gps_altitude_m,
                "speed_kmh": self.gps_speed_kmh,
                "fixed_at": self.gps_fixed_at,
                "message": self.gps_message,
            },
            "activity": {
                "last_at_command": self.last_at_command,
                "last_at_command_at": self.last_at_command_at,
                "last_at_response": self.last_at_response,
                "last_at_response_at": self.last_at_response_at,
                "at_commands_sent": self.at_commands_sent,
                "at_timeouts": self.at_timeouts,
                "reconnects": self.reconnects,
                "error_count": self.error_count,
                "last_error": self.last_error,
                "last_error_at": self.last_error_at,
            },
            "updated_at": self.updated_at,
            "status_line": self.status_line(),
        }


class StateBox:
    """Lock-guarded holder so readers never observe a half-updated modem state."""

    def __init__(self, state: Sim808State | None = None) -> None:
        self._lock = threading.RLock()
        self._state = state or Sim808State()

    def update(self, mutator) -> None:
        """Apply *mutator(state)* under the lock."""

        with self._lock:
            mutator(self._state)

    def read(self, selector=None):
        with self._lock:
            return selector(self._state) if selector else self._state

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._state.to_dict()

    @property
    def state(self) -> Sim808State:
        return self._state
