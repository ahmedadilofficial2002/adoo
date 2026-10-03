"""SIM808 manager - the sole owner of the modem serial port.

Rules enforced here:

* Only this class opens the COM port. Other threads go through the ``send_at``
  passthrough under a lock instead of opening their own handle.
* Opening a port is not "online": state reports online only after the module
  answers ``AT`` with ``OK``.
* Every command has a timeout and bounded retries. A dead modem costs a timeout,
  never the voice loop.
* A background watchdog marks the module offline when the port or the handshake
  dies and keeps trying to bring it back without blocking the rest of the app.
* State lives in :class:`~vb.hardware.sim808_state.Sim808State` (single source of
  truth for the dashboard and the AI); changes are published on the EventBus.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Callable

from . import at_parsers as at
from . import nmea
from .sim808_compat import (GpsFix, Sim808Error, SmsMessage, parse_cgpsinfo,
                            parse_cmgl, to_fix, to_message)
from .sim808_state import UNKNOWN, Sim808State, StateBox

try:  # pragma: no cover - exercised implicitly
    import serial
    from serial.tools import list_ports
except ImportError:  # pyserial is optional so the rest of the app still runs
    serial = None
    list_ports = None

#: The tool layer imports its value objects from this module; keep that working.
__all__ = ["Sim808Manager", "Sim808Error", "GpsFix", "SmsMessage", "parse_cgpsinfo",
           "parse_cmgl", "list_modem_ports", "guess_modem_port"]

DEFAULT_BAUDRATE = 115200
HANDSHAKE_TIMEOUT = 3.0
COMMAND_TIMEOUT = 5.0
COMMAND_RETRIES = 2
READ_CHUNK = 256
MAX_RESPONSE_BYTES = 65536
RX_BUFFER_LIMIT = 4096
RETRY_DELAY = 5.0
SMS_SUBMIT_TIMEOUT = 30.0      # network acknowledgement can take ~20s on 2G

SUB = "\x1a"           # ctrl-Z: submit the SMS body on SIM808
ESC = "\x1b"           # ctrl-[: cancel the SMS body

#: Sent once the module answers AT. Each entry must be safe on a cold modem.
INIT_COMMANDS = [
    "ATE0",                    # stop echoing commands back at us
    "AT+CMEE=2",               # verbose error codes instead of bare ERROR
    "AT+CSCLK=0",              # never auto-sleep the UART (breaks wake-ups)
    "AT+CMGF=1",               # text mode SMS
    "AT+CSMP=17,167,0,0",      # text-mode parameter defaults
    "AT+CNMI=2,2,0,0,0",       # route new messages here and ring +CMTI
    "AT+CLIP=1",               # report the caller number on incoming calls
    "AT+CREG=2",               # URC when network registration changes
    "AT+CGREG=2",              # URC when GPRS registration changes
]

#: Read-only queries that rebuild the dashboard state. Safe to re-run any time.
STATUS_COMMANDS = [
    "AT+CPIN?", "AT+CSQ", "AT+CREG?", "AT+CGREG?", "AT+COPS?",
    "AT+CGATT?", "AT+CFUN?", "AT+CBC?", "AT+CGMR", "AT+CGSN", "AT+CMGF?",
]


def response_prefix(command: str) -> str:
    """The ``+XXX`` tag a command answers with, used to tell answer from URC.

    ``AT+CPIN?`` answers ``+CPIN: READY`` - the very same tag the module also
    reports on its own when the SIM is removed. Without knowing what was asked
    for, a status query's own answer would be eaten by the URC handler.
    """

    text = (command or "").strip().upper()
    if not text.startswith("AT+"):
        return ""
    body = text[3:]
    end = len(body)
    for separator in "?=:,/":
        position = body.find(separator)
        if position != -1:
            end = min(end, position)
    return "+" + body[:end] if end else ""



class Sim808Manager:
    """Owns the serial link, publishes state, and answers AT requests."""

    def __init__(self, config=None, bus=None, state_box: StateBox | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        modem = getattr(config, "modem", None) if config else None
        self.port: str = str(getattr(modem, "port", "")
                             or getattr(config, "sim808_port", "") or "")
        self.baudrate: int = int(getattr(modem, "baudrate", 0)
                                 or getattr(config, "sim808_baudrate", 0)
                                 or DEFAULT_BAUDRATE)
        self.retry_seconds: float = float(
            getattr(modem, "retry_seconds", 0) or RETRY_DELAY)
        self.command_timeout: float = float(
            getattr(config, "sim808_timeout", 0) or COMMAND_TIMEOUT)
        max_retries = getattr(modem, "max_retries", None)
        self.max_retries: int | None = None if max_retries in (None, 0) else int(max_retries)

        self.bus = bus
        self.box = state_box or StateBox()
        self.state: Sim808State = self.box.state
        self.state.port = self.port
        self.state.baudrate = self.baudrate
        self._clock = clock

        self._ser = None
        self._rx: deque[str] = deque(maxlen=RX_BUFFER_LIMIT)   # unread module lines
        self._io_lock = threading.RLock()      # one AT transaction at a time
        self._lock = threading.RLock()         # guards object attributes
        self._watch: threading.Thread | None = None
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._on_line: Callable[[str], None] | None = None
        self._inbox: deque[dict[str, Any]] = deque(maxlen=100)
        self._sms_charset = ""
        self._clip = ""                        # last +CLIP caller number
        self._pending_cmt: str | None = None   # +CMT: header awaiting its body line
        self._awaiting = ""                    # +XXX tag of the answer in flight
        self._gps_reading: dict[str, Any] | None = None   # accumulated NMEA fix
        self._pumping = False                  # guards nested URC handling
        self._pending_cmti: deque[int] = deque(maxlen=64)   # SMS storage indexes
        self._fake = None                      # set by FakeSim808.attach()

    # ================================================================ state
    @property
    def connected(self) -> bool:
        """True only while the port is open AND the module answers AT."""

        return self.state.online

    def is_online(self) -> bool:
        return self.state.online

    def get_state(self) -> Sim808State:
        return self.state

    def get_status(self) -> dict[str, Any]:
        """Full snapshot for the dashboard plus the legacy flat keys."""

        snapshot = self.box.snapshot()
        snapshot.update({
            "connected": snapshot["online"],
            "signal_strength": snapshot["network"]["signal_rssi"],
            "battery_level": snapshot["power"]["battery_percent"],
            "registration_status": snapshot["network"]["registration_csd"],
            "unread_sms": snapshot["sms"]["unread"],
            "last_error": snapshot["activity"]["last_error"],
            "gps_position": None if not snapshot["gps"]["valid"] else [
                snapshot["gps"]["latitude"], snapshot["gps"]["longitude"]],
        })
        return snapshot

    @property
    def incoming_messages(self) -> list[dict[str, Any]]:
        """Recent inbound SMS, newest first (kept for the AI tools)."""

        with self._lock:
            return list(self._inbox)[::-1]

    def _publish(self, name: str, payload: dict[str, Any] | None = None) -> None:
        if self.bus is None:
            return
        try:
            self.bus.publish(name, dict(payload or {}))
        except Exception:  # pragma: no cover - bus is fault-isolated already
            pass

    def _set(self, **changes: Any) -> None:
        """Apply changes to the shared state under its lock."""

        def apply(st: Sim808State) -> None:
            for key, value in changes.items():
                setattr(st, key, value)
            st.touch()

        self.box.update(apply)

    def _note_error(self, message: str) -> None:
        self.box.update(lambda st: st.note_error(message))

    # ============================================================= connect
    def start(self) -> bool:
        """Bring the modem up without blocking, then keep it up forever."""

        self._stop.clear()
        online = self.connect()
        self._watch = threading.Thread(target=self._watchdog_loop,
                                       name="sim808-watchdog", daemon=True)
        self._watch.start()
        return online

    def connect(self) -> bool:
        """Open the port and handshake, retrying up to ``max_retries``.

        Never raises: a missing or dead modem must not stop the app. Returns
        True only on a real ``AT`` -> ``OK`` handshake.
        """

        with self._io_lock:
            attempts = 0
            reason = ""
            while not self._stop.is_set():
                attempts += 1
                if self._open_and_handshake():
                    return True
                reason = self.state.last_error or reason    # keep why it failed
                if self.max_retries is not None and attempts >= self.max_retries:
                    break
                if self._watch is None or self._watch is threading.current_thread():
                    break           # without a watchdog one call is one attempt
                self._stop.wait(self.retry_seconds)
            detail = reason.strip() or (
                f"no AT handshake on {self.port or 'no port configured'}")
            self._set(port=self.port, baudrate=self.baudrate)
            self.box.update(lambda st: st.mark_offline(
                f"SIM808 offline after {attempts} attempt(s): {detail}"))
            self._publish("modem.offline", {"port": self.port, "attempts": attempts})
            return False

    def connect_and_configure(self) -> bool:
        """Backwards-compatible alias used by the older entry points."""

        return self.connect()

    def wait_online(self, timeout: float = 10.0) -> bool:
        return self._connected.wait(timeout)

    def _open_and_handshake(self) -> bool:
        if serial is None and self._fake is None:
            self.box.update(lambda st: st.mark_offline("pyserial is not installed"))
            return False
        if not self.port and self._fake is None:
            self.box.update(lambda st: st.mark_offline("no modem port configured"))
            return False
        self._close_port()
        try:
            self._ser = serial.Serial(port=self.port, baudrate=self.baudrate,
                                      timeout=0.2, write_timeout=2.0)
        except Exception as exc:
            self.box.update(lambda st: st.mark_offline(
                f"cannot open {self.port}: {type(exc).__name__}: {exc}"))
            return False

        self._rx.clear()
        self._set(serial_open=True, module_responding=False, online=False,
                  port=self.port, baudrate=self.baudrate)
        for reset in ("reset_input_buffer", "reset_output_buffer"):
            try:
                getattr(self._ser, reset)()
            except Exception:
                pass
        time.sleep(0.3)
        self._drain()
        if not self._handshake():
            self._close_port()
            self.box.update(lambda st: st.mark_offline(
                f"{self.port} opened but AT got no OK within {HANDSHAKE_TIMEOUT}s"))
            return False
        self._configure()
        return True

    def _handshake(self) -> bool:
        """AT must answer OK. Nothing counts as online until it does."""

        for _ in range(3):
            if "OK" in self._transact("AT", timeout=HANDSHAKE_TIMEOUT).upper():
                self._set(serial_open=True, module_responding=True, online=True)
                self._connected.set()
                self._publish("modem.online", {"port": self.port})
                return True
            time.sleep(0.2)
        self._connected.clear()
        return False

    def _configure(self) -> None:
        """Apply the init command group. Failures are recorded, never fatal."""

        failed = []
        text_mode = False
        for command in INIT_COMMANDS:
            response = self._command(command, timeout=COMMAND_TIMEOUT)
            if "OK" not in response.upper():
                failed.append(f"{command} -> {response.strip() or 'no reply'}")
            elif command.upper() == "AT+CMGF=1":
                text_mode = True       # the module accepted text mode, not assumed
        if text_mode:
            self._set(sms_format="TEXT")
        self._sms_charset = "GSM"
        self.refresh_status()
        if failed:
            self._note_error("init rejected: " + "; ".join(failed)[:280])

    def _close_port(self) -> None:
        ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        self._connected.clear()

    def stop(self) -> None:
        """Close the port and stop the watchdog. Safe to call twice."""

        self._stop.set()
        watch, self._watch = self._watch, None
        if watch is not None and watch.is_alive():
            watch.join(timeout=2.0)
        with self._io_lock:
            self._close_port()
            self.box.update(lambda st: st.mark_offline("SIM808 closed by application"))
            self._publish("modem.offline", {"port": self.port, "reason": "stopped"})

    close = stop

    # =========================================================== watchdog
    def _watchdog_loop(self) -> None:
        """Poll AT on an interval and reconnect forever when the link dies."""

        while not self._stop.is_set():
            if self.state.online:
                if self._stop.wait(self.retry_seconds):
                    return
                if "OK" in self._transact("AT", timeout=HANDSHAKE_TIMEOUT).upper():
                    self.refresh_status(light=True)
                    continue
                self._close_port()
                self.box.update(lambda st: st.mark_offline(
                    "SIM808 stopped answering AT on " + self.port))
                self._publish("modem.offline", {"port": self.port, "reason": "no AT reply"})
            self._set(reconnects=self.state.reconnects + 1)
            with self._io_lock:
                if self._stop.wait(self.retry_seconds):
                    return
                if self._open_and_handshake():
                    self._publish("modem.reconnected", {"port": self.port})
                    continue
                self.box.update(lambda st: st.mark_offline(
                    f"SIM808 still offline; retrying every {self.retry_seconds:.0f}s"))

    # ==================================================== serial IO layer
    def _transact(self, command: str, timeout: float = COMMAND_TIMEOUT,
                  wait_for: str = "", end: str = "\r") -> str:
        """Write one command and collect lines until a terminal response.

        Unsolicited results (URCs) seen mid-transaction are routed to
        ``_handle_urc`` instead of being mistaken for the answer. The tag this
        command answers with is remembered while it runs, so a query's own
        ``+CPIN: READY`` report is not swallowed as an unsolicited one.
        """

        ser = self._ser
        if ser is None:
            return ""
        self._awaiting = response_prefix(command)
        try:
            return self._transact_locked(ser, command, timeout, wait_for, end)
        finally:
            self._awaiting = ""

    def _transact_locked(self, ser: Any, command: str, timeout: float,
                         wait_for: str, end: str) -> str:
        try:
            ser.write((command + end).encode("utf-8", errors="ignore"))
            try:
                ser.flush()
            except Exception:
                pass
        except Exception as exc:
            self._io_failure(exc)
            return ""

        lines: list[str] = []
        size = 0
        pending = ""
        deadline = self._clock() + timeout
        target = wait_for.upper()
        while self._clock() < deadline:
            try:
                chunk = ser.read(READ_CHUNK)
            except Exception as exc:
                self._io_failure(exc)
                break
            if not chunk:
                continue
            pending += chunk.decode("utf-8", errors="ignore")
            while "\n" in pending:
                raw, pending = pending.split("\n", 1)
                line = raw.strip()
                if not line:
                    continue
                size += len(line)
                if size > MAX_RESPONSE_BYTES:
                    return "\r\n".join(lines)
                if self._feed(line):
                    continue
                lines.append(line)
                upper = line.upper()
                if target and upper.startswith(target):
                    return "\r\n".join(lines)
                if upper in ("OK", "ERROR") or upper.startswith(("+CMS ERROR", "+CME ERROR")):
                    return "\r\n".join(lines)
        return "\r\n".join(lines)

    def _command(self, command: str, timeout: float = COMMAND_TIMEOUT,
                 retries: int = COMMAND_RETRIES, wait_for: str = "") -> str:
        """Run *command* with a timeout and bounded retries. Never raises."""

        if timeout == COMMAND_TIMEOUT:
            timeout = self.command_timeout      # config sim808_timeout wins here
        response = ""
        for attempt in range(retries + 1):
            with self._io_lock:
                response = self._transact(command, timeout=timeout, wait_for=wait_for)
            self.box.update(lambda st: st.note_at(command, response))
            if response.strip():
                break
            if attempt >= retries or not self.state.online:
                break
        if not response.strip():
            def timed_out(st: Sim808State) -> None:
                st.at_timeouts += 1
                st.note_error(f"{command} timed out after {timeout:.1f}s")
            self.box.update(timed_out)
        elif "ERROR" in response.upper():
            self._note_error(f"{command} -> {response.strip()[:200]}")
        if not self._pumping:
            self._pump_messages()
        return response

    def send_at(self, command: str, timeout: float = COMMAND_TIMEOUT,
                wait_for: str = "") -> str:
        """The only AT entry point other threads may use (dashboard, AI)."""

        if not self.state.online:
            return ""
        return self._command(str(command), timeout=timeout, wait_for=wait_for)

    def _drain(self) -> int:
        """Consume whatever the module already sent; returns the line count."""

        ser = self._ser
        if ser is None:
            return 0
        count = 0
        try:
            while getattr(ser, "in_waiting", 0):
                raw = ser.readline()
                if not raw:
                    break
                count += 1
                line = raw.decode("utf-8", errors="ignore").strip()
                if line:
                    self._feed(line)
        except Exception as exc:
            self._io_failure(exc)
        return count

    def _io_failure(self, exc: Exception) -> None:
        """The port died mid-transaction: say so once, let the watchdog fix it."""

        if not self.state.serial_open:
            return
        self._close_port()
        self.box.update(lambda st: st.mark_offline(
            f"serial error on {self.port}: {type(exc).__name__}: {exc}"))
        self._publish("modem.offline", {"port": self.port, "reason": str(exc)[:200]})

    # ======================================================== URC handling
    def _is_urc(self, line: str) -> bool:
        """Is this line something the module volunteered?"""

        upper = line.upper()
        if self._awaiting and upper.startswith(self._awaiting):
            return False                # it is the answer to the command in flight
        if upper.startswith("$G"):
            return True
        if self.state.gps_enabled and upper.startswith("+GPS"):
            return True
        if upper.startswith(("+CMTI", "+CMT:", "+CMT,", "+CLIP", "+CONNECT",
                             "+CREG:", "+CGREG:", "+CPIN:", "+PBMS")):
            return True
        return upper in ("RING", "NO CARRIER", "NO ANSWER", "BUSY", "NO DIALTONE",
                         "CALL END", "CALL ENDED", "VOICE OVER GSM CALL")

    def _handle_urc(self, line: str) -> None:
        """Keep the central state truthful about calls, SIM, and messages."""

        upper = line.upper()
        try:
            if upper.startswith("+CLIP"):
                self._clip = at.parse_clip(line)
                self._set(call_number=self._clip)
                return
            if upper == "RING":
                self._set(call_state="RINGING", call_direction="IN",
                          call_number=self._clip, call_since=time.time(),
                          call_duration_seconds=0.0)
                self._publish("call.incoming", {"number": self._clip})
                return
            if upper.startswith("+CONNECT") or upper == "VOICE OVER GSM CALL":
                self._set(call_state="ACTIVE", call_since=time.time())
                self._publish("call.connected", {"number": self.state.call_number})
                return
            if upper in ("NO CARRIER", "NO ANSWER", "BUSY", "NO DIALTONE",
                         "CALL END", "CALL ENDED"):
                self._end_call(upper.replace(" ", "_").lower())
                return
            if upper.startswith("+CMTI"):
                index = at.parse_cmti(line)
                if index is not None:
                    self._pending_cmti.append(index)
                return
            if upper.startswith("+CMT:"):
                self._pending_cmt = line      # the body arrives on the next line
                return
            if upper.startswith("+CPIN"):
                status = at.parse_cpin(line)
                ready = status == "READY"
                self._set(sim_status=status, sim_ready=ready)
                self._publish("sim.ready" if ready else "sim.blocked", {"status": status})
                return
            if upper.startswith(("+CREG:", "+CGREG:")):
                self._apply_registration(line)
                return
            if self.state.gps_enabled and upper.startswith(("$G", "+GPS")):
                self.update_gps(line)
        except Exception as exc:  # a malformed URC must not kill the read loop
            self._note_error(f"URC parse error on {line[:60]}: {exc}")

    def _apply_registration(self, line: str) -> None:
        """One +CREG/+CGREG line (poll answer or URC) moves the registration."""

        csd = self.state.registration_csd
        gprs = self.state.registration_gprs
        if line.upper().startswith("+CREG"):
            csd = at.parse_creg(line)
        else:
            gprs = at.parse_cgreg(line)
        registered = at.is_registered(csd, gprs)
        self._set(registration_csd=csd, registration_gprs=gprs, registered=registered)
        self._publish("network.registered" if registered else "network.lost",
                      {"csd": csd, "gprs": gprs})

    def _end_call(self, reason: str) -> None:
        state = self.state
        if state.call_state in ("", "IDLE", "ENDED"):
            return
        ended = time.time()
        duration = max(0.0, ended - state.call_since) if state.call_since else 0.0
        number = state.call_number
        self._set(call_state="ENDED", call_duration_seconds=duration,
                  last_call_number=number, last_call_at=ended,
                  last_call_duration=duration, last_call_reason=reason)
        self._publish("call.ended", {"number": number, "reason": reason,
                                     "duration_seconds": round(duration, 1)})
        self._clip = ""

    # ======================================================= status refresh
    def refresh_status(self, light: bool = False) -> dict[str, Any]:
        """Re-read the module and rebuild the published state.

        ``light`` is the cheap variant the watchdog runs between polls: signal
        and registration only. The full variant also reads identity, battery and
        storage and is run after init, after a reconnect, or on dashboard
        request. Returns the snapshot (empty when offline).
        """

        if not self.state.online:
            return {}
        commands = ["AT+CSQ", "AT+CREG?", "AT+CGREG?"] if light else list(STATUS_COMMANDS)
        for command in commands:
            self.apply_status(command, self._command(command, timeout=COMMAND_TIMEOUT))
        if not light:
            self.apply_identity(self._command("ATI", timeout=COMMAND_TIMEOUT))
            self.apply_status("AT+CNUM?", self._command("AT+CNUM?", timeout=COMMAND_TIMEOUT))
            self.apply_status("AT+CPMS?", self._command("AT+CPMS?", timeout=COMMAND_TIMEOUT))
            self.apply_status("AT+CIMI", self._command("AT+CIMI", timeout=COMMAND_TIMEOUT))
            self.apply_status("AT+CCID", self._command("AT+CCID", timeout=COMMAND_TIMEOUT))
        self._set(online=True, serial_open=True, module_responding=True)
        self._publish("modem.status", {"light": light, **self.box.snapshot()})
        return self.box.snapshot()

    def check_sim_status(self) -> str:
        """AT+CPIN? -> READY / PIN REQUIRED / NOT INSERTED / UNKNOWN."""

        status = at.parse_cpin(self._command("AT+CPIN?", timeout=COMMAND_TIMEOUT))
        self._set(sim_status=status, sim_ready=status == "READY")
        return status

    def check_network_status(self) -> dict[str, Any]:
        """Signal + registration + operator in one round of queries."""

        for command in ("AT+CSQ", "AT+CREG?", "AT+CGREG?", "AT+COPS?", "AT+CGATT?", "AT+CFUN?"):
            self.apply_status(command, self._command(command, timeout=COMMAND_TIMEOUT))
        return self.box.snapshot()["network"]

    def check_battery(self) -> dict[str, Any]:
        self.apply_status("AT+CBC?", self._command("AT+CBC?", timeout=COMMAND_TIMEOUT))
        snapshot = self.box.snapshot()["power"]
        self._publish("modem.battery", dict(snapshot))
        return snapshot

    def apply_status(self, command: str, response: str) -> None:
        """Fold one status response into the state. Empty response = no change."""

        if not response.strip():
            return
        upper = command.upper()
        if upper.startswith("AT+CPIN"):
            status = at.parse_cpin(response)
            self._set(sim_status=status, sim_ready=status == "READY")
        elif upper.startswith("AT+CSQ"):
            rssi, ber = at.parse_csq(response)
            self._set(signal_rssi=rssi, bit_error_rate=ber,
                      signal_percent=at.signal_percent(rssi))
        elif upper.startswith("AT+CREG?"):
            csd = at.parse_creg(response)
            self._set(registration_csd=csd,
                      registered=at.is_registered(csd, self.state.registration_gprs))
        elif upper.startswith("AT+CGREG?"):
            gprs = at.parse_cgreg(response)
            self._set(registration_gprs=gprs,
                      registered=at.is_registered(self.state.registration_csd, gprs))
        elif upper.startswith("AT+COPS"):
            operator, _rat = at.parse_cops(response)
            if operator:
                self._set(operator=operator)
        elif upper.startswith("AT+CGATT"):
            self._set(gprs_attached=at.parse_cgatt(response))
        elif upper.startswith("AT+CFUN"):
            enabled = at.parse_cfun(response)
            self._set(radio_disabled=None if enabled is None else not enabled)
        elif upper.startswith("AT+CBC"):
            percent, volts, charging = at.parse_cbc(response)
            changes = {}
            if percent is not None:
                changes["battery_percent"] = percent
            if volts is not None:
                changes["battery_volts"] = volts
            if charging is not None:
                changes["charging"] = charging
            if changes:
                self._set(**changes)
        elif upper.startswith("AT+CGMR"):
            firmware = at.last_value(response, "") or response.strip()
            if firmware:
                self._set(firmware=firmware[:60])
        elif upper.startswith("AT+CGSN"):
            imei = at.parse_imei(response)
            if imei:
                self._set(imei=imei)
        elif upper.startswith("AT+CIMI"):
            imsi = at.parse_cimi(response)
            if imsi:
                self._set(imsi=imsi)
        elif upper.startswith("AT+CCID"):
            iccid = at.parse_ccid(response)
            if iccid:
                self._set(iccid=iccid)
        elif upper.startswith("AT+CMGF"):
            value = at.last_value(response, "+CMGF")
            if value:
                self._set(sms_format="TEXT" if value.startswith("1") else "PDU")
        elif upper.startswith("AT+CNUM"):
            msisdn = at.parse_cnum(response)
            if msisdn:
                self._set(msisdn=msisdn)
        elif upper.startswith("AT+CPMS"):
            parts = [p.strip().strip('"') for p in at.last_value(response, "+CPMS").split(",")]
            if parts:
                self._set(sms_storage=parts[0])

    def apply_identity(self, response: str) -> None:
        """Fold the ATI revision block into model/firmware fields."""

        info = at.parse_ati(response)
        changes = {}
        if info.get("model"):
            changes["module_model"] = info["model"][:60]
        if info.get("revision"):
            changes["firmware"] = info["revision"][:60]
        if changes:
            self._set(**changes)

    # ================================================================== SMS
    def send_sms(self, number: str, text: str) -> tuple[bool, str]:
        """Submit one text message and report honestly whether it went out.

        ``True`` is returned only when the module answers ``+CMGS: <ref>``, its
        own proof of submission to the network. A timeout, a missing ``>`` prompt,
        or a bare ``OK`` is reported as failure with the module's words, so
        nothing upstream can claim a send that did not happen.
        """

        number = str(number or "").strip()
        text = str(text or "").strip()
        if not text:
            ok, detail = False, "nothing to send: message is empty"
        elif not number:
            ok, detail = False, "nothing to send: no recipient number"
        elif not self.state.online:
            ok, detail = False, "SIM808 is offline: " + (self.state.last_error or "no AT handshake")
        else:
            self._ensure_text_mode()
            body = self._prepare_body(text)
            with self._io_lock:
                prompt = self._transact(f'AT+CMGS="{number}"',
                                        timeout=COMMAND_TIMEOUT, wait_for=">")
                if ">" not in prompt:
                    ok = False
                    detail = (prompt.strip() or
                              f"module did not accept recipient {number}")[:300]
                else:
                    response = self._transact(body, timeout=SMS_SUBMIT_TIMEOUT,
                                              wait_for="+CMGS", end=SUB)
                    ok, detail = at.parse_cmgs(response)
        now = time.time()

        def record(st: Sim808State) -> None:
            st.last_sms_send_ok = ok
            st.last_sms_send_at = now
            st.last_sms_error = "" if ok else detail[:300]
            if not ok:
                st.note_error(f"SMS to {number or 'unknown'} failed: {detail[:200]}")
            st.touch()

        self.box.update(record)
        self._publish("sms.sent", {"to": number, "ok": ok, "detail": detail,
                                   "characters": len(text)})
        return ok, detail

    def _ensure_text_mode(self) -> None:
        if self.state.sms_format != "TEXT":
            if "OK" in self._command("AT+CMGF=1", timeout=COMMAND_TIMEOUT).upper():
                self._set(sms_format="TEXT")

    def _prepare_body(self, text: str) -> str:
        """Keep the GSM alphabet on the wire; hex-encode UCS-2 only when needed."""

        if text.isascii():
            if self._sms_charset != "GSM":
                if "OK" in self._command('AT+CSCS="GSM"', timeout=COMMAND_TIMEOUT).upper():
                    self._sms_charset = "GSM"
            return text
        if self._sms_charset != "UCS2":
            if "OK" not in self._command('AT+CSCS="UCS2"', timeout=COMMAND_TIMEOUT).upper():
                # The module refused UCS-2: send the ASCII skeleton rather than
                # silently corrupting the text, so the user knows what went out.
                self._sms_charset = "GSM"
                return text.encode("ascii", "ignore").decode("ascii")
            self._sms_charset = "UCS2"
        return text.encode("utf-16-be").hex().upper()

    def set_line_callback(self, callback: Callable[[str], None] | None) -> None:
        """Receive every raw line the module sends (dashboard AT log)."""

        self._on_line = callback

    def _feed(self, line: str) -> bool:
        """Route one received line. True means it was not part of an answer."""

        self._rx.append(line)                     # raw transcript, bounded deque
        callback = self._on_line
        if callback is not None:
            try:
                callback(line)
            except Exception:                     # a bad listener must not stop reads
                pass
        upper = line.upper()
        if self._pending_cmt is not None:
            header, self._pending_cmt = self._pending_cmt, None
            sender, timestamp = at.parse_header(header)
            if upper in ("OK", "ERROR") or self._is_urc(line):
                # Empty body: what arrived next is not the message text.
                self._register_message({"index": None, "from": sender,
                                        "timestamp": timestamp, "text": ""})
                if upper.startswith("+CMT:"):
                    self._pending_cmt = line
                elif self._is_urc(line):
                    self._handle_urc(line)
                return True
            self._register_message({"index": None, "from": sender,
                                    "timestamp": timestamp, "text": line.strip()})
            return True
        if upper.startswith("+CMT:"):
            self._pending_cmt = line      # body arrives on the next line
            return True
        if self._is_urc(line):
            self._handle_urc(line)
            return True
        return False

    def _register_message(self, message: dict[str, Any]) -> None:
        """Store one inbound message and announce it on the bus exactly once."""

        message.setdefault("received_at", time.time())
        message.setdefault("from", "")
        message.setdefault("text", "")
        with self._lock:
            duplicate = any(
                previous.get("index") is not None
                and previous.get("index") == message.get("index")
                and previous.get("text") == message.get("text")
                for previous in self._inbox)
            if duplicate:
                return
            self._inbox.append(message)
        self._set(unread_sms=self.state.unread_sms + 1,
                  last_sms_from=message.get("from", ""), last_sms_at=time.time())
        self._publish("sms.received", dict(message))

    def _pump_messages(self) -> list[dict[str, Any]]:
        """Read the +CMTI indexes queued while another transaction was running."""

        if self._pumping or not self._pending_cmti or not self.state.online:
            return []
        self._pumping = True
        read: list[dict[str, Any]] = []
        try:
            while self._pending_cmti:
                index = self._pending_cmti.popleft()
                message = self.read_message(index)
                if message:
                    read.append(message)
        finally:
            self._pumping = False
        return read

    def read_message(self, index: int) -> dict[str, Any] | None:
        """AT+CMGR=<index> -> message dict (header parsed, body kept as-is)."""

        response = self._command(f"AT+CMGR={index}", timeout=COMMAND_TIMEOUT)
        header = ""
        body: list[str] = []
        for line in at.clean_lines(response):
            if line.upper().startswith("+CMGR"):
                header = line
            elif header:
                body.append(line)
        if not header:
            return None
        sender, timestamp = at.parse_header(header)
        message = {"index": index, "from": sender, "timestamp": timestamp,
                   "text": "\n".join(body).strip()}
        self._register_message(message)
        return message

    def read_inbox(self, limit: int = 10) -> list[dict[str, Any]]:
        """Unread messages, newest first. Empty list (never an error) offline."""

        if self.state.online:
            self._ensure_text_mode()
            response = self._command('AT+CMGL="REC UNREAD"', timeout=COMMAND_TIMEOUT)
            for message in reversed(at.parse_cmgl(response)):
                self._register_message(message)
            self._set(unread_sms=min(len(self._inbox), 999))
        return self.incoming_messages[:max(1, int(limit))]

    def delete_message(self, index: int) -> bool:
        """AT+CMGD=<index>. True only when the module confirms with OK."""

        ok = "OK" in self._command(f"AT+CMGD={index}", timeout=COMMAND_TIMEOUT).upper()
        if ok:
            self._set(unread_sms=max(0, self.state.unread_sms - 1))
        return ok

    def clear_inbox(self) -> bool:
        """Delete every stored message (AT+CMGD=0,4 wipes the storage)."""

        ok = "OK" in self._command("AT+CMGD=0,4", timeout=SMS_SUBMIT_TIMEOUT).upper()
        if ok:
            self._set(unread_sms=0)
        return ok

    # =============================================================== calls
    def dial(self, number: str) -> tuple[bool, str]:
        """ATD<number>; - start a voice call. Returns (accepted, detail)."""

        number = str(number or "").strip()
        if not number:
            return False, "no number to dial"
        if not self.state.online:
            return False, "SIM808 is offline: " + (self.state.last_error or "no AT handshake")
        if self.state.call_state not in ("", "IDLE", "ENDED"):
            return False, f"a call is already {self.state.call_state.lower()}"
        response = self._command(f"ATD{number};", timeout=COMMAND_TIMEOUT)
        upper = response.upper()
        if "OK" not in upper:
            reason = next((word for word in ("BUSY", "NO CARRIER", "NO ANSWER",
                                             "NO DIALTONE", "ERROR")
                           if word in upper), "")
            self._set(call_state="IDLE", call_number="")
            return False, (reason or response.strip() or "module rejected the dial command")[:300]
        self._set(call_state="OUTGOING", call_direction="OUT", call_number=number,
                  call_since=time.time(), call_duration_seconds=0.0)
        self._publish("call.dialing", {"number": number})
        self._pump_call_setup(2.0)
        return True, "dialing " + number

    def answer(self) -> tuple[bool, str]:
        """ATA - pick up an incoming call."""

        if not self.state.online:
            return False, "SIM808 is offline"
        if self.state.call_state in ("", "IDLE", "ENDED") and not self._clip:
            return False, "there is no incoming call to answer"
        response = self._command("ATA", timeout=COMMAND_TIMEOUT)
        if "OK" in response.upper():
            self._set(call_state="ACTIVE", call_since=time.time())
            self._publish("call.connected", {"number": self.state.call_number})
            return True, "answered" + (
                " " + self.state.call_number if self.state.call_number else "")
        return False, (response.strip() or "module rejected the answer command")[:300]

    def hang_up(self) -> tuple[bool, str]:
        """ATH - release the line, then confirm the module agrees it ended."""

        if not self.state.online:
            return False, "SIM808 is offline"
        response = self._command("ATH", timeout=COMMAND_TIMEOUT)
        if "OK" not in response.upper():
            return False, (response.strip() or "module rejected the hang-up command")[:300]
        self._end_call("local_hangup")
        self._set(call_state="IDLE", call_number="", call_direction="", call_since=0.0)
        return True, "call ended"

    def check_call_status(self) -> dict[str, Any]:
        """AT+CLCC? - ask the module what it thinks is happening."""

        if not self.state.online:
            return self.box.snapshot()["call"]
        info = at.parse_clcc(self._command("AT+CLCC", timeout=COMMAND_TIMEOUT))
        if info:
            state = info.get("state") or self.state.call_state
            changes = {"call_state": state, "call_number": info.get("number")
                       or self.state.call_number}
            if state == "ACTIVE" and self.state.call_state != "ACTIVE":
                changes["call_since"] = time.time()
            if state == "IDLE" and self.state.call_state not in ("", "IDLE"):
                changes["call_state"] = "ENDED"
            self._set(**changes)
        return self.call_status()

    def call_status(self) -> dict[str, Any]:
        """Current call snapshot with a live duration while the call is up."""

        if self.state.call_state == "ACTIVE" and self.state.call_since:
            self._set(call_duration_seconds=max(
                0.0, time.time() - self.state.call_since))
        return self.box.snapshot()["call"]

    def _pump_call_setup(self, seconds: float) -> None:
        """Listen briefly after ATD so CONNECT / NO CARRIER land in the state."""

        deadline = self._clock() + seconds
        while self._clock() < deadline and self.state.call_state == "OUTGOING":
            self._drain()
            time.sleep(0.05)

    # ================================================================== GPS
    #: Power-on sequence. Later commands are optional on some firmware builds,
    #: so a refusal is reported but does not abort the sequence.
    GPS_START_COMMANDS = ["AT+CGNSPWR=1", "AT+CGNSSTT=0,5,0,0", "AT+CGNSCFG=138"]

    def gps_start(self) -> tuple[bool, str]:
        """Power the GPS engine and enable NMEA output. Never raises."""

        if not self.state.online:
            return False, "SIM808 is offline: " + (self.state.last_error or "no AT handshake")
        refused = [command for command in self.GPS_START_COMMANDS
                   if "OK" not in self._command(command, timeout=COMMAND_TIMEOUT).upper()]
        value = at.last_value(self._command("AT+CGNSPWR?", timeout=COMMAND_TIMEOUT),
                              "+CGNSPWR")
        powered = value == "1"
        detail = "GPS powered on" if powered else "module did not power the GPS"
        if refused:
            detail += " (rejected: " + ", ".join(refused) + ")"
        self._set(gps_enabled=powered, gps_valid=False if not powered else self.state.gps_valid,
                  gps_message="" if powered else detail)
        if powered:
            self._publish("gps.enabled", {"refused": refused})
        return powered, detail

    def gps_stop(self) -> tuple[bool, str]:
        if not self.state.online:
            return False, "SIM808 is offline"
        ok = "OK" in self._command("AT+CGNSPWR=0", timeout=COMMAND_TIMEOUT).upper()
        self._set(gps_enabled=False, gps_valid=False,
                  gps_message="GPS powered off" if ok else "module kept GPS powered")
        return ok, "GPS powered off" if ok else "module refused to power down the GPS"

    def gps_position(self, refresh: bool = True) -> dict[str, Any]:
        """Last known fix. ``refresh`` asks the module with AT+CGNSINF first."""

        if refresh and self.state.online and self.state.gps_enabled:
            self.update_gps(self._command("AT+CGNSINF", timeout=COMMAND_TIMEOUT))
        return self.box.snapshot()["gps"]

    def read_nmea(self, seconds: float = 3.0) -> list[dict[str, Any]]:
        """Collect raw NMEA sentences for *seconds* and fold them into state.

        Raw output routing is firmware dependent; when the module does not emit
        sentences, this returns an empty list and ``gps_position()`` stays the
        authoritative source instead of inventing a fix.
        """

        if not self.state.online:
            return []
        readings: list[dict[str, Any]] = []
        deadline = self._clock() + max(0.0, seconds)
        while self._clock() < deadline:
            ser = self._ser
            if ser is None:
                break
            try:
                raw = ser.readline() if getattr(ser, "in_waiting", 0) else b""
            except Exception as exc:
                self._io_failure(exc)
                break
            if not raw:
                time.sleep(0.02)
                continue
            line = raw.decode("utf-8", errors="ignore").strip()
            if not line.upper().startswith("$G"):
                continue
            reading = nmea.parse(line)
            if reading is not None:
                readings.append(reading)
                self.update_gps(line)
        return readings

    def update_gps(self, line: str) -> dict[str, Any] | None:
        """Apply one ``+CGNSINF`` answer or NMEA sentence to the state."""

        text = (line or "").strip()
        if not text:
            return None
        changes: dict[str, Any] = {}
        if text.upper().startswith("+CGNSINF"):
            info = at.parse_cgnsinf(text)
            if not info["powered"] and not info["valid"]:
                self._set(gps_message="GPS not running")
                return None
            valid = bool(info["valid"]) and info["latitude"] is not None
            changes = {
                "gps_valid": valid,
                "gps_latitude": info["latitude"] if valid else None,
                "gps_longitude": info["longitude"] if valid else None,
                "gps_altitude_m": info["altitude_m"] if valid else None,
                "gps_speed_kmh": info["speed_kmh"] if valid else None,
                "gps_message": "" if valid else "searching for satellites",
            }
        else:
            reading = nmea.parse(text)
            if reading is None:
                return None            # bad checksum or unsupported sentence
            self._gps_reading = nmea.merge(self._gps_reading, reading)
            merged = self._gps_reading
            changes = {
                "gps_valid": bool(merged.get("valid")),
                "gps_latitude": merged.get("latitude"),
                "gps_longitude": merged.get("longitude"),
                "gps_altitude_m": merged.get("altitude_m"),
                "gps_speed_kmh": merged.get("speed_kmh"),
                "gps_message": "" if merged.get("valid") else "searching for satellites",
            }
        was_valid = self.state.gps_valid
        if changes.get("gps_valid"):
            changes["gps_fixed_at"] = time.time()
        self._set(**changes)
        if changes.get("gps_valid") and not was_valid:
            self._publish("gps.fix", {k: changes.get(k) for k in
                                      ("gps_latitude", "gps_longitude",
                                       "gps_altitude_m", "gps_speed_kmh")})
        return changes

    # ==================================== legacy surface (tools/, main.py)
    @property
    def last_error(self) -> str | None:
        """None while the link is clean, otherwise the last thing that failed."""

        return self.state.last_error or None

    @property
    def signal_strength(self) -> int:
        """Raw CSQ value (0-31, 99 = undetectable)."""

        return self.state.signal_rssi

    def get_gps(self, force: bool = False) -> GpsFix:
        """Position for the AI tools. ``force`` powers up the GPS and re-reads."""

        if force and self.state.online and not self.state.gps_enabled:
            self.gps_start()
        self.gps_position(refresh=force)
        return self.box.read(to_fix)

    def start_gps(self) -> bool:
        """True when the module confirmed that the GPS engine is powered."""

        return self.gps_start()[0]

    def stop_gps(self) -> bool:
        return self.gps_stop()[0]

    def hangup(self) -> tuple[bool, str]:
        """Alias of :meth:`hang_up` kept for :mod:`vb.tools.calls`."""

        return self.hang_up()

    def reject(self) -> tuple[bool, str]:
        """Refuse a ringing call. ATH first, AT+CHUP on firmwares that refuse."""

        if not self.state.online:
            return False, "SIM808 is offline"
        response = self._command("ATH", timeout=COMMAND_TIMEOUT)
        if "OK" not in response.upper():
            response = self._command("AT+CHUP", timeout=COMMAND_TIMEOUT)
        ok = "OK" in response.upper()
        if ok:
            self._end_call("rejected")
            self._set(call_state="IDLE", call_number="", call_direction="")
            return True, "call rejected"
        return False, (response.strip() or "module refused to reject the call")[:300]

    def list_sms(self, mode: str = "ALL") -> list[SmsMessage]:
        """Read the module's SMS storage for :mod:`vb.tools.sms`.

        Raises :class:`Sim808Error` when the storage cannot be read, instead of
        handing back an empty list that would look like "no messages".
        """

        if not self.state.online:
            raise Sim808Error("SIM808 is offline: "
                              + (self.state.last_error or "no AT handshake"))
        self._ensure_text_mode()
        wanted = str(mode or "ALL").strip().upper().strip('"')
        if wanted not in ("ALL", "REC UNREAD", "REC READ", "STOR UNREAD", "UNSENT", "SENT"):
            wanted = "ALL"
        response = self._command(f'AT+CMGL="{wanted}"', timeout=COMMAND_TIMEOUT)
        upper = response.upper()
        if not response.strip():
            raise Sim808Error(f"no answer to AT+CMGL=\"{wanted}\"")
        if "+CMGL" not in upper and "ERROR" in upper:
            raise Sim808Error(response.strip()[:200])
        items = at.parse_cmgl(response)
        for item in reversed(items):
            self._register_message(item)
        return [to_message(item) for item in items]

    def module_info(self) -> str:
        """One readable status line for the "check the modem" voice intent."""

        state = self.state
        if not state.online:
            return ("SIM808 unavailable: "
                    + (state.last_error or "the module never answered AT"))
        bits = [f"SIM808 online on {state.port} at {state.baudrate} baud",
                f"signal {state.signal_percent}% (CSQ {state.signal_rssi})",
                f"network {state.operator or state.registration_csd}",
                f"SIM {state.sim_status}"]
        if state.battery_percent is not None:
            bits.append(f"battery {state.battery_percent}%")
        if state.imei:
            bits.append(f"IMEI {state.imei}")
        if state.firmware:
            bits.append(f"firmware {state.firmware}")
        bits.append(f"{state.unread_sms} unread SMS")
        bits.append(f"call {state.call_state}")
        return "; ".join(bits) + "."


# ----------------------------------------------------------- port discovery
#: Substrings that mark a serial port as probably the GSM module rather than an
#: Arduino, a GPS puck or a Bluetooth COM bridge.
MODEM_HINTS = ("sim8", "sim7", "gsm", "quad-band", "modem", "qualcomm", "huawei",
               "atcom", "tpsa", "usb gsm")


def list_modem_ports() -> list[dict[str, Any]]:
    """All serial ports, likely modems first. Empty list if pyserial is absent."""

    if list_ports is None:
        return []
    try:
        found = list(list_ports.comports())
    except Exception:
        return []
    ports = [{"device": info.device,
              "description": info.description or "",
              "likely_modem": looks_like_modem(info.description or "", info.device)}
             for info in found]
    ports.sort(key=lambda port: (not port["likely_modem"], port["device"]))
    return ports


def looks_like_modem(description: str, device: str) -> bool:
    text = f"{description} {device}".lower()
    return any(hint in text for hint in MODEM_HINTS)


def guess_modem_port() -> str:
    """The single most likely SIM808 port, or "" when nothing looks like one."""

    for port in list_modem_ports():
        if port["likely_modem"]:
            return str(port["device"])
    return ""
