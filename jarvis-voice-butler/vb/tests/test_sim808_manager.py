"""Hardware-free tests for :class:`Sim808Manager`.

A scripted fake replaces pyserial, so these tests exercise the real transaction,
URC and state code paths: what the manager reports is what a module answered.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vb.hardware import sim808_manager as module
from vb.hardware.sim808_manager import (GpsFix, Sim808Error, Sim808Manager,
                                        SmsMessage, looks_like_modem, parse_cgpsinfo)

#: Answers the fake module gives, keyed by the exact command received.
SCRIPT = {
    "AT": ["OK"],
    "ATI": ["SIM808 R2", "Revision: SIM800_R21_1801B", "OK"],
    "AT+CGSN": ["867311054321098", "OK"],
    "AT+CIMI": ["621300000000000", "OK"],
    "AT+CCID": ["+ICCID: 8925078910000123456", "OK"],
    "AT+CPIN?": ["+CPIN: READY", "OK"],
    "AT+CSQ": ["+CSQ: 22,99", "OK"],
    "AT+CREG?": ["+CREG: 2,1", "OK"],
    "AT+CGREG?": ["+CGREG: 2,1", "OK"],
    "AT+COPS?": ['+COPS: 0,0,"Safaricom",7', "OK"],
    "AT+CGATT?": ["+CGATT: 1", "OK"],
    "AT+CFUN?": ["+CFUN: 1", "OK"],
    "AT+CBC?": ["+CBC: 0,87", "OK"],
    "AT+CGMR": ["Revision: SIM800_R21_1801B", "OK"],
    "AT+CNUM?": ['+CNUM: "","+254700111222",145', "OK"],
    "AT+CPMS?": ['+CPMS: "SM",2,10,"SM",2,10,"SM",2,10', "OK"],
    "AT+CMGF=1": ["+CMGF: 1", "OK"],
    'AT+CMGL="REC UNREAD"': ['+CMGL: 1,"REC UNREAD","+254700111222","","26/09/28,10:00:00+03"',
                             "Water tank is full", "OK"],
    'AT+CMGL="ALL"': ['+CMGL: 1,"REC UNREAD","+254700111222","","26/09/28,10:00:00+03"',
                      "Water tank is full",
                      '+CMGL: 2,"REC READ","+254700999888","","26/09/28,09:00:00+03"',
                      "Pump switched off", "OK"],
    "AT+CMGR=3": ['+CMGR: "REC UNREAD","+254700555666","","26/09/28,11:30:00+03"',
                  "Gate opened at 11:30", "OK"],
    "AT+CGNSPWR=1": ["OK"],
    "AT+CGNSPWR?": ["+CGNSPWR: 1", "OK"],
    "AT+CGNSINF": ["+CGNSINF: 1,1,2026,09,28,10,15,20.00,1.292078,36.821945,1795.0,"
                   "0.50,90.0,A,,0.98,1.40,1.20,,10,8,0,,28,100,95", "OK"],
    "ATD+254700555666;": ["OK", "VOICE OVER GSM CALL"],
    "AT+CLCC": ['+CLCC: 1,0,0,0,0,"+254700555666",145,"",0', "OK"],
    "AT+CSCA?": ['+CSCA: "+254700000000",145', "OK"],
}


class FakeModem:
    """pyserial-shaped SIM808: answers scripted replies, queues unsolicited ones."""

    def __init__(self, script: dict[str, list[str]] | None = None) -> None:
        self.script = dict(SCRIPT)
        self.script.update(script or {})
        self.port: str = ""
        self.baudrate: int = 0
        self.closed = False
        self.sent: list[str] = []            # every line written by the manager
        self._out = bytearray()              # bytes awaiting the next read
        self._queue: list[list[str]] = []    # unsolicited blocks to emit

    # ---------------------------------------------------------- pyserial API
    @property
    def in_waiting(self) -> int:
        self._pump_queue()
        return len(self._out)

    def read(self, size: int = 1) -> bytes:
        self._pump_queue()
        if not self._out:
            time.sleep(0.002)                # never spin the CPU like a real port
            return b""
        chunk = bytes(self._out[:max(1, size)])
        del self._out[:len(chunk)]
        return chunk

    def readline(self) -> bytes:
        self._pump_queue()
        if not self._out:
            return b""
        index = self._out.find(0x0A)
        if index < 0:
            index = len(self._out) - 1
        line = bytes(self._out[:index + 1])
        del self._out[:index + 1]
        return line

    def write(self, data: bytes) -> int:
        self._pump_queue()                   # anything the module volunteered
        # already sits in the buffer ahead of our command
        text = data.decode("utf-8", errors="ignore")
        if "\x1a" in text:                   # Ctrl-Z submits the SMS body
            self.sent.append("SUBMIT:" + text.replace("\x1a", "").strip())
            self._emit(self.script.get("SUBMIT", ["+CMGS: 7", "OK"]))
            return len(data)
        self.sent.append(text.strip())
        if text.endswith("\r"):
            command = text.strip().upper()
            lines = self.script.get(command)
            if lines is None:
                # a real module answers a number it accepts with a prompt
                lines = [">"] if command.startswith('AT+CMGS="') else ["OK"]
            self._emit(lines)
        return len(data)

    def flush(self) -> None:
        return None

    def reset_input_buffer(self) -> None:
        self._out.clear()

    def reset_output_buffer(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    # ------------------------------------------------------------- scripting
    def _emit(self, lines: list[str]) -> None:
        for line in lines:
            self._out.extend((line + "\r\n").encode("utf-8"))

    def _pump_queue(self) -> None:
        if self._queue and not self._out:
            self._emit(self._queue.pop(0))

    def inject(self, *lines: str) -> None:
        """Queue unsolicited lines the manager must pick up on its next read."""

        self._queue.append(list(lines))

    def commands(self) -> list[str]:
        return [entry for entry in self.sent if not entry.startswith("SUBMIT:")]


class FakeSerialModule:
    """Replaces the ``serial`` module inside sim808_manager."""

    def __init__(self, modem: FakeModem, error: Exception | None = None) -> None:
        self.modem = modem
        self.error = error
        self.attempts = 0

    def Serial(self, port=None, baudrate=0, timeout=None, write_timeout=None, **_: Any):
        self.attempts += 1
        if self.error is not None:
            raise self.error
        self.modem.port = port
        self.modem.baudrate = baudrate
        return self.modem


class FakeConfig:
    class _Modem:
        port = "COM9"
        baudrate = 115200
        retry_seconds = 0.05

    modem = _Modem()
    sim808_timeout = 0.6


def bring_up(script: dict[str, list[str]] | None = None,
             error: Exception | None = None) -> tuple[Sim808Manager, FakeModem]:
    """Wire a fake module into the manager and run the real connect sequence."""

    modem = FakeModem(script)
    fake = FakeSerialModule(modem, error)
    original = module.serial
    module.serial = fake  # type: ignore[assignment]
    manager = Sim808Manager(FakeConfig())
    try:
        manager.connect()
    finally:
        module.serial = original  # type: ignore[assignment]
    manager._fake_serial = fake          # keep the handle for assertions
    return manager, modem


# ============================================================== the happy path
def test_online_only_after_a_real_handshake() -> None:
    manager, modem = bring_up()
    state = manager.state
    assert state.online and state.serial_open and state.module_responding
    assert state.owner == "sim808_manager"
    assert modem.port == "COM9" and modem.baudrate == 115200
    assert state.imei == "867311054321098"
    assert state.firmware == "SIM800_R21_1801B"
    assert "SIM808" in state.module_model
    assert state.iccid == "8925078910000123456"
    assert state.imsi == "621300000000000"
    assert state.msisdn == "+254700111222"
    assert state.signal_rssi == 22 and state.signal_percent == 71   # 22/31 of the CSQ scale
    assert state.operator == "Safaricom"
    assert state.registered and state.registration_csd == "REGISTERED_HOME"
    assert state.sim_ready and state.sim_status == "READY"
    assert state.battery_percent == 87
    assert state.gprs_attached and state.radio_disabled is False
    assert state.sms_format == "TEXT"          # confirmed by +CMGF: 1
    assert state.gps_enabled is False          # never claimed without CGNSPWR? = 1
    assert "AT+CMGF=1" in modem.commands()
    manager.stop()


def test_text_mode_is_armed_once_and_rearmed_after_a_reset() -> None:
    manager, modem = bring_up()
    assert manager.state.sms_format == "TEXT"
    before = modem.commands().count("AT+CMGF=1")
    manager._ensure_text_mode()               # already TEXT: must not re-send
    assert modem.commands().count("AT+CMGF=1") == before
    manager._set(sms_format="")               # firmware dropped back to PDU
    manager._ensure_text_mode()
    assert modem.commands().count("AT+CMGF=1") == before + 1
    assert manager.state.sms_format == "TEXT"
    manager.stop()


# ==================================================================== honesty
def test_a_port_that_cannot_be_opened_is_reported_as_offline() -> None:
    manager, _ = bring_up(error=OSError(5, "Access is denied"))
    assert manager.state.online is False
    assert manager.state.serial_open is False
    assert "Access is denied" in (manager.last_error or "")
    assert manager.send_sms("+254700123456", "boiler is on")[0] is False
    assert manager.dial("+254700123456")[0] is False
    assert manager.gps_start()[0] is False
    with pytest.raises(Sim808Error):
        manager.list_sms()
    fix = manager.get_gps()
    assert fix.valid is False and "NOT AVAILABLE" in fix.message
    assert "unavailable" in manager.module_info().lower()


def test_ok_without_cmgs_is_not_reported_as_sent() -> None:
    manager, _ = bring_up({"SUBMIT": ["OK"]})    # module accepts but never submits
    ok, detail = manager.send_sms("+254700123456", "pump on")
    assert ok is False
    assert "+CMGS" in detail.upper()
    assert manager.state.last_sms_send_ok is False
    assert "no +CMGS" in manager.state.last_sms_error or "CMGS" in manager.state.last_sms_error
    manager.stop()


def test_cms_error_from_the_module_is_passed_through() -> None:
    manager, _ = bring_up({"SUBMIT": ["+CMS ERROR: 500"]})
    ok, detail = manager.send_sms("+254700123456", "pump on")
    assert ok is False and "500" in detail
    assert manager.state.last_sms_send_ok is False
    manager.stop()


def test_no_prompt_means_the_number_was_refused() -> None:
    manager, modem = bring_up({'AT+CMGS="+254700123456"': ['+CME ERROR: 500']})
    ok, detail = manager.send_sms("+254700123456", "pump on")
    assert ok is False and "500" in detail
    assert "SUBMIT:" not in " ".join(modem.sent)     # body never written
    manager.stop()


# ============================================================ receiving messages
def test_a_cmti_alert_is_followed_by_the_stored_message() -> None:
    manager, modem = bring_up()
    assert manager.incoming_messages == []
    modem.inject('+CMTI: "SM",3')
    manager.send_at("AT")                       # any transaction drains the queue
    messages = manager.incoming_messages
    assert len(messages) == 1
    assert messages[0]["index"] == 3
    assert messages[0]["text"] == "Gate opened at 11:30"
    assert messages[0]["from"] == "+254700555666"
    assert manager.state.unread_sms == 1
    assert manager.state.last_sms_from == "+254700555666"
    assert "AT+CMGR=3" in modem.commands()      # the alert made it read the storage
    manager.send_at("AT")                       # a repeat must not double-count
    assert len(manager.incoming_messages) == 1
    manager.stop()


def test_a_directly_delivered_message_keeps_sender_and_body_together() -> None:
    manager, modem = bring_up()
    modem.inject('+CMT: "+254700333444","","26/09/28,11:45:00+03"', "Generator fuel low")
    manager._drain()                            # same path the read loop uses
    message = manager.incoming_messages[0]
    assert message["from"] == "+254700333444"
    assert message["text"] == "Generator fuel low"
    assert manager.state.last_sms_from == "+254700333444"
    assert manager.state.call_state == "IDLE"   # a message is never a call
    manager.stop()


def test_list_sms_returns_objects_and_keeps_the_status() -> None:
    manager, modem = bring_up()
    messages = manager.list_sms("ALL")
    assert [m.body for m in messages] == ["Water tank is full", "Pump switched off"]
    assert all(isinstance(m, SmsMessage) for m in messages)
    assert messages[0].index == 1 and messages[0].status == "REC UNREAD"
    assert messages[0].sender == "+254700111222"
    assert messages[1].status == "REC READ"
    assert "AT+CMGL=\"ALL\"" in modem.commands()
    assert len(manager.incoming_messages) == 2
    manager.stop()


def test_an_unread_inbox_is_read_without_losing_messages() -> None:
    manager, _ = bring_up()
    inbox = manager.read_inbox(limit=5)
    assert len(inbox) == 1
    assert inbox[0]["text"] == "Water tank is full"
    assert manager.state.unread_sms == 1
    assert manager.delete_message(inbox[0]["index"]) is True
    assert manager.state.unread_sms == 0
    manager.stop()


# ======================================================================== calls
def test_an_incoming_call_is_tracked_from_ring_to_carrier() -> None:
    manager, modem = bring_up()
    modem.inject('+CLIP: "+254700555666",145,,,,0', "RING")
    manager._drain()
    state = manager.state
    assert state.call_state == "RINGING"
    assert state.call_direction == "IN"
    assert state.call_number == "+254700555666"
    ok, detail = manager.answer()
    assert ok and "+254700555666" in detail
    assert manager.state.call_state == "ACTIVE"
    assert "ATA" in modem.commands()
    modem.inject("NO CARRIER")
    manager._drain()
    assert manager.state.call_state == "ENDED"
    assert manager.state.last_call_reason == "no_carrier"
    assert manager.state.last_call_number == "+254700555666"
    manager.stop()


def test_a_dial_is_only_reported_dialing_after_the_module_agrees() -> None:
    manager, modem = bring_up({"ATD+254712345678;": ["OK"]})
    ok, detail = manager.dial("+254712345678")
    assert ok and detail == "dialing +254712345678"
    assert manager.state.call_state == "OUTGOING"
    assert manager.state.call_direction == "OUT"
    assert manager.dial("+254799999999")[0] is False      # one line at a time
    assert manager.state.call_number == "+254712345678"
    ok, detail = manager.hangup()
    assert ok and detail == "call ended"
    assert manager.state.call_state == "IDLE"
    assert manager.state.last_call_reason == "local_hangup"
    assert "ATH" in modem.commands()
    manager.stop()


def test_a_rejected_dial_never_reports_a_call() -> None:
    manager, _ = bring_up({"ATD+254712345678;": ["ERROR"]})
    ok, detail = manager.dial("+254712345678")
    assert ok is False and detail
    assert manager.state.call_state == "IDLE"
    assert manager.state.call_number == ""
    assert manager.answer()[0] is False         # nothing to pick up
    manager.stop()


def test_reject_hangs_up_a_ringing_call() -> None:
    manager, modem = bring_up()
    modem.inject("RING")
    manager._drain()
    assert manager.state.call_state == "RINGING"
    ok, detail = manager.reject()
    assert ok and detail == "call rejected"
    assert manager.state.call_state == "IDLE"
    assert manager.state.last_call_reason == "rejected"
    assert "ATH" in modem.commands()
    manager.stop()


# ======================================================================== GPS
def nmea_sentence(body: str) -> str:
    """Attach the correct checksum so a test sentence looks like module output."""

    check = 0
    for character in body:
        check ^= ord(character)
    return f"${body}*{check:02X}"


def test_gps_is_only_reported_after_the_module_powers_it() -> None:
    manager, modem = bring_up()
    assert manager.state.gps_enabled is False
    assert manager.get_gps().valid is False          # nothing claimed yet
    assert manager.start_gps() is True
    assert manager.state.gps_enabled is True
    assert "AT+CGNSPWR=1" in modem.commands()
    fix = manager.get_gps(force=True)
    assert isinstance(fix, GpsFix) and fix.valid
    assert fix.latitude == pytest.approx(1.292078, abs=1e-6)
    assert fix.longitude == pytest.approx(36.821945, abs=1e-6)
    assert fix.altitude_m == pytest.approx(1795.0)
    assert fix.speed_kmh == pytest.approx(0.93, abs=0.01)   # 0.5 knots, not km/h
    assert fix.message == "" and fix.timestamp
    assert manager.stop_gps() is True
    assert manager.state.gps_enabled is False
    assert manager.get_gps().valid is False
    manager.stop()


def test_searching_for_satellites_is_never_reported_as_a_position() -> None:
    searching = ("+CGNSINF: 1,0,2026,09,28,10,15,20.00,0.000000,0.000000,0.0,0.00,0.00,"
                 "A,,9.99,9.99,9.99,,0,0,0,,0,0,0")
    manager, _ = bring_up({"AT+CGNSINF": [searching, "OK"]})
    assert manager.start_gps() is True
    position = manager.gps_position()
    assert position["valid"] is False
    assert position["latitude"] is None              # zeros are not a location
    assert manager.state.gps_message == "searching for satellites"
    assert manager.get_gps().valid is False
    assert manager.box.snapshot()["gps"]["valid"] is False
    manager.stop()


def test_nmea_sentences_are_trusted_only_with_a_valid_checksum() -> None:
    manager, modem = bring_up()
    manager.start_gps()                              # now $G lines feed the state
    rmc = nmea_sentence("GNRMC,101521.00,A,0117.5247,N,03649.3167,E,0.50,90.00,280926,,,A")
    gga = nmea_sentence("GNGGA,101520.00,0117.5247,N,03649.3167,E,1,08,0.98,1795.0,M,0,M,,")
    broken = rmc[:-1] + ("0" if rmc[-1] != "0" else "1")
    modem.inject(broken)
    assert manager.read_nmea(0.05) == []             # a corrupt line is dropped
    assert manager.state.gps_valid is False
    modem.inject(gga, rmc)
    readings = manager.read_nmea(0.3)
    assert [reading["type"] for reading in readings] == ["GGA", "RMC"]
    assert manager.state.gps_valid is True
    assert manager.state.gps_latitude == pytest.approx(1.292078, abs=1e-6)
    assert manager.state.gps_longitude == pytest.approx(36.821945, abs=1e-6)
    assert manager.state.gps_altitude_m == pytest.approx(1795.0)
    assert manager.state.gps_speed_kmh == pytest.approx(0.93, abs=0.01)
    manager.stop()


def test_legacy_gps_and_gnss_transcripts_still_parse() -> None:
    fix = parse_cgpsinfo("+CGPSINFO: 0117.5247,N,03649.3167,E,280926,101520.00,1795.0,0.50,90.00")
    assert fix.valid and fix.latitude == pytest.approx(1.292078, abs=1e-6)
    assert fix.timestamp == "2026-09-28 10:15:20"
    assert parse_cgpsinfo("+CGPSINFO: ,,,,,,,,,").valid is False
    old_firmware = module.at.parse_cgnsinf(
        "+CGNSINF: 1,1,20260928101520.000,1.292100,36.821900,1795.0,0.50,90.0,,A,10,28,1.4,2.0,1.4")
    assert old_firmware["valid"]
    assert old_firmware["latitude"] == pytest.approx(1.2921, abs=1e-6)
    assert old_firmware["longitude"] == pytest.approx(36.8219, abs=1e-6)
    assert module.at.parse_cgnsinf("+CGNSINF: 0,0")["powered"] is False
    assert module.at.parse_cgnsinf("")["valid"] is False


# ====================================================== dashboard and tool face
def test_the_dashboard_snapshot_is_json_and_keeps_the_legacy_keys() -> None:
    import json

    manager, _ = bring_up()
    snapshot = manager.get_status()
    json.dumps(snapshot)                            # must survive a websocket hop
    for key in ("connected", "signal_strength", "battery_level", "registration_status",
                "unread_sms", "last_error", "gps_position"):
        assert key in snapshot, key
    assert snapshot["connected"] is True
    assert snapshot["signal_strength"] == 22
    assert snapshot["battery_level"] == 87
    assert snapshot["registration_status"] == "REGISTERED_HOME"
    assert snapshot["gps_position"] is None
    assert snapshot["owner"] == "sim808_manager"
    assert snapshot["online"] is True
    manager.stop()


def test_module_info_reads_like_a_status_report() -> None:
    manager, _ = bring_up()
    info = manager.module_info()
    assert "COM9" in info and "115200" in info
    assert "Safaricom" in info and "SIM READY" in info
    assert "IMEI 867311054321098" in info
    assert "firmware SIM800_R21_1801B" in info
    assert "signal 71% (CSQ 22)" in info
    assert manager.last_error is None               # a clean boot leaves nothing to blame
    assert manager.signal_strength == 22
    manager.stop()


def test_every_raw_line_reaches_the_line_callback() -> None:
    manager, modem = bring_up()
    seen: list[str] = []
    manager.set_line_callback(seen.append)
    modem.inject("RING", '+CMTI: "SM",3')
    manager.send_at("AT")
    assert "RING" in seen and '+CMTI: "SM",3' in seen
    assert "RING" in "".join(manager._rx)           # kept for the AT transcript view
    manager.set_line_callback(None)
    quiet = len(seen)
    manager.send_at("AT")
    assert len(seen) == quiet                       # detached listeners stay silent
    manager.stop()


def test_a_module_that_stops_answering_is_timed_out_not_hung() -> None:
    started = time.monotonic()
    manager, _ = bring_up({"AT+CBC?": []})          # battery read goes silent
    manager.refresh_status(light=True)
    manager.send_at("AT+CBC?", timeout=0.2)
    assert manager.state.at_timeouts >= 1
    assert "timed out" in (manager.last_error or "")
    assert manager.state.online is True             # one quiet command is not a dead link
    assert manager.state.at_commands_sent >= 1
    assert time.monotonic() - started < 20          # bounded by timeouts, not forever
    manager.stop()


def test_registration_urcs_move_the_state_without_a_poll() -> None:
    manager, modem = bring_up()
    assert manager.state.registered is True
    modem.inject("+CREG: 2,0")
    manager._drain()
    assert manager.state.registered is False
    assert manager.state.registration_csd == "NOT REGISTERED"
    modem.inject("+CREG: 2,5")
    manager._drain()
    assert manager.state.registered is True
    assert manager.state.registration_csd == "REGISTERED_ROAMING"
    modem.inject("+CPIN: SIM PIN")
    manager._drain()
    assert manager.state.sim_ready is False
    assert manager.state.sim_status == "PIN REQUIRED"
    manager.stop()


def test_port_discovery_recognises_a_modem_description() -> None:
    assert looks_like_modem("USB Serial Port (COM5)", "COM5") is False
    assert looks_like_modem("SIM800 Board", "COM7") is True
    assert looks_like_modem("Qualcomm HS-USB Diagnostics", "COM3") is True
    assert module.guess_modem_port.__doc__          # discovery stays documented
    assert module.list_modem_ports.__module__ == module.__name__


def main() -> int:
    """Run every test in this module without pytest (``python -m`` friendly)."""

    failures = 0
    for name, function in sorted(globals().items()):
        if not name.startswith("test_") or not callable(function):
            continue
        try:
            function()
        except Exception as exc:  # noqa: BLE001 - the runner reports, never hides
            failures += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {name}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())




