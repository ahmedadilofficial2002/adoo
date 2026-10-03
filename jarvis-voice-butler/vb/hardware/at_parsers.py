"""Pure AT-response parsers.

The manager used to hand raw AT echoes (whole ``ATI`` blocks) to callers, which
forced the dashboard and the AI to guess. Every value the dashboard shows is
parsed here instead. These functions are pure and hardware-free on purpose so
they can be unit-tested with recorded modem transcripts.
"""

from __future__ import annotations

import re

#: AT+CREG? / +CGREG? / +CEREG? stat values.
REGISTRATION_LABELS = {
    "0": "NOT REGISTERED",
    "1": "REGISTERED_HOME",
    "2": "SEARCHING",
    "3": "REGISTRATION_DENIED",
    "4": "UNKNOWN",
    "5": "REGISTERED_ROAMING",
}

#: AT+CPIN? values, normalised.
SIM_STATUS_LABELS = {
    "READY": "READY",
    "SIM PIN": "PIN REQUIRED",
    "SIM PUK": "PUK REQUIRED",
    "PH-SIM PIN": "PH-SIM PIN",
    "SIM NOT INSERTED": "NOT INSERTED",
    "SIM REMOVED": "REMOVED",
    "ERROR": "ERROR",
}

#: 3GPP TS 27.007 access technology reported by AT+COPS?.
RAT_LABELS = {
    "0": "GSM", "1": "COMPACT", "2": "UTRAN", "3": "GSM-EDGE",
    "6": "UTRAN-HSDPA", "7": "UTRAN-HSUPA", "8": "UTRAN-HSDPA-HSUPA",
    "13": "LTE", "15": "NR",
}


def clean_lines(response: str) -> list[str]:
    """Non-empty, stripped lines with command echoes and terminators removed."""

    skip = {"OK", "ERROR", ""}
    lines = []
    for raw in (response or "").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if line and line not in skip and not line.upper().startswith("AT"):
            lines.append(line)
    return lines


def last_value(response: str, prefix: str) -> str:
    """Return the payload of the last line starting with *prefix*."""

    found = ""
    upper = prefix.upper()
    for line in clean_lines(response):
        if line.upper().startswith(upper):
            found = line.split(":", 1)[1].strip() if ":" in line else line
    return found.strip('"').strip()


def parse_ati(response: str) -> dict[str, str]:
    """ATI revision block -> {"model": ..., "revision": ...}."""

    out = {"model": "", "revision": ""}
    for line in clean_lines(response):
        upper = line.upper()
        if upper.startswith(("SIM8", "SIM7", "SIM9")):
            out["model"] = line
            match = re.search(r"R(\d+\.\d+)", line)
            if match:
                out["revision"] = "R" + match.group(1)
        elif upper.startswith("REVISION") and ":" in line:
            out["revision"] = line.split(":", 1)[1].strip()
        elif upper.startswith("MODEL") and ":" in line:
            out["model"] = line.split(":", 1)[1].strip()
    return out


def parse_csq(response: str) -> tuple[int, str]:
    """AT+CSQ -> (rssi 0-31|99, bit error rate string)."""

    parts = [p.strip() for p in last_value(response, "+CSQ").split(",")]
    if not parts or not parts[0]:
        return 99, "99"
    try:
        rssi = int(parts[0])
    except ValueError:
        return 99, "99"
    return max(0, min(99, rssi)), (parts[1] if len(parts) > 1 else "99")


def signal_percent(rssi: int | None) -> int:
    """Map the 0-31 CSQ scale onto a 0-100% bar. 99/None means unknown."""

    if rssi is None or int(rssi) < 0 or int(rssi) >= 99:
        return 0
    return max(0, min(100, round(int(rssi) * 100 / 31)))


def parse_cpin(response: str) -> str:
    """AT+CPIN? -> normalised SIM status."""

    value = last_value(response, "+CPIN")
    if not value:
        return "NOT INSERTED" if "ERROR" in (response or "").upper() else "UNKNOWN"
    return SIM_STATUS_LABELS.get(value.upper(), value.upper())


def _registration_stat(response: str, prefix: str) -> str:
    """CREG/CGREG return "<n>,<stat>" or sometimes just "<stat>"."""

    parts = [p.strip() for p in last_value(response, prefix).split(",")]
    if len(parts) >= 2:
        return parts[1]
    return parts[0] if parts and parts[0] else ""


def parse_creg(response: str) -> str:
    return REGISTRATION_LABELS.get(_registration_stat(response, "+CREG"), "UNKNOWN")


def parse_cgreg(response: str) -> str:
    return REGISTRATION_LABELS.get(_registration_stat(response, "+CGREG"), "UNKNOWN")


def is_registered(csd: str, gprs: str = "") -> bool:
    """Is the modem on the network for voice and SMS?

    ``AT+CREG`` (circuit switched) decides, because calls and text messages need
    it. The ``AT+CGREG`` label only breaks the tie while CREG has not answered
    yet, so a definite "NOT REGISTERED" is never hidden behind a stale GPRS
    value - that would keep reporting a dead link as registered.
    """

    for label in (csd, gprs):
        text = str(label or "").strip().upper()
        if not text or text == "UNKNOWN":
            continue                    # nothing said yet: ask the other one
        return text.startswith("REGISTERED")
    return False


def parse_cops(response: str) -> tuple[str, str]:
    """AT+COPS? -> (operator name, radio access technology)."""

    # payload looks like: 0,0,"MTN-RWANDA",7
    parts = [p.strip() for p in last_value(response, "+COPS").split(",")]
    operator = ""
    for part in parts:
        if part and not part.strip('"').lstrip("-").isdigit():
            operator = part.strip('"')
            break
    rat = RAT_LABELS.get(parts[-1].strip('"'), parts[-1].strip('"')) if len(parts) >= 4 else ""
    return operator, rat


def parse_cgatt(response: str) -> bool:
    """AT+CGATT? -> packet domain attached."""

    return last_value(response, "+CGATT") == "1"


def parse_cfun(response: str) -> bool | None:
    """AT+CFUN? -> True when the radio is fully enabled, False if minimal."""

    value = last_value(response, "+CFUN")
    if value == "1":
        return True
    if value == "0":
        return False
    return None


def parse_cbc(response: str) -> tuple[int | None, float | None, bool | None]:
    """AT+CBC -> (percent, volts, charging)."""

    parts = [p.strip() for p in last_value(response, "+CBC").split(",") if p.strip()]
    if not parts:
        return None, None, None
    percent: int | None = None
    volts: float | None = None
    charging: bool | None = None
    if len(parts) == 1:
        percent = _to_int(parts[0])
    else:
        charging = parts[0] in {"1", "2"}
        percent = _to_int(parts[1])
        if len(parts) >= 3:
            millivolts = _to_int(parts[2])
            volts = round(millivolts / 1000.0, 2) if millivolts is not None else None
    if percent is not None:
        percent = max(0, min(100, percent))
    return percent, volts, charging


def parse_imei(response: str) -> str:
    """AT+CGSN -> the 15-digit IMEI, not the echoed command."""

    for line in clean_lines(response):
        if re.fullmatch(r"\d{14,17}", line):
            return line
    return ""


def parse_cimi(response: str) -> str:
    for line in clean_lines(response):
        if re.fullmatch(r"\d{14,15}", line):
            return line
    return ""


def parse_ccid(response: str) -> str:
    """AT+CCID -> the ICCID. Some firmwares label it +ICCID, some print raw digits."""

    value = last_value(response, "+ICCID") or last_value(response, "+CCID")
    if not value:
        for line in clean_lines(response):
            if re.fullmatch(r"\d{10,22}", line):
                value = line
                break
    return value if re.fullmatch(r"\d{10,22}", value or "") else ""


def parse_cnum(response: str) -> str:
    """AT+CNUM? -> stored MSISDN (frequently blank on prepaid SIMs)."""

    for line in clean_lines(response):
        match = re.search(r'"(\+?\d[\d ]{5,20})"', line)
        if match:
            return match.group(1).replace(" ", "")
    return ""


def parse_cmgs(response: str) -> tuple[bool, str]:
    """Decide whether AT+CMGS really submitted the message to the network.

    Honesty rule: never call a message sent just because a command was written.
    Success is the module's own ``+CMGS: <mr>`` reference. The ``>`` prompt, a
    command echo, or a bare ``OK`` (which also comes back from ``AT+CMGF=1``)
    are not proof of submission.
    """

    text = response or ""
    upper = text.upper()
    if "ERROR" in upper:
        match = re.search(r"\+CMS ERROR:\s*(\d+)", upper)
        reason = f" (CMS error {match.group(1)})" if match else ""
        return False, (text.strip() or f"module rejected the message{reason}")[:300]
    match = re.search(r"\+CMGS:\s*(\d+)", upper)
    if match:
        return True, f"submitted to network (reference {match.group(1)})"
    if "+CMGS" in upper:
        return True, "submitted to network"
    leftover = text.strip()
    if leftover.upper() in ("", "OK"):     # a bare OK proves nothing
        leftover = ""
    return False, (leftover or "no +CMGS reference from module; delivery unconfirmed")[:300]


def parse_clip(response: str) -> str:
    match = re.search(r'\+CLIP:\s*"([^"]*)"', response or "")
    return match.group(1).strip() if match else ""


def parse_cmti(response: str) -> int | None:
    """+CMTI: "SM",12 -> 12 (the storage index of a new message)."""

    match = re.search(r'\+CMTI:\s*"[^"]+",\s*(\d+)', response or "", re.I)
    return int(match.group(1)) if match else None


def parse_header(header: str) -> tuple[str, str]:
    """Pull (sender, timestamp) out of a +CMT/+CMGR header line."""

    quoted = re.findall(r'"([^"]*)"', header or "")
    sender = ""
    timestamp = ""
    for item in quoted:
        if not item:
            continue
        if "/" in item and ":" in item and not timestamp:
            timestamp = item
        elif (item.startswith("+") or (item.isdigit() and len(item) >= 8)) and not sender:
            sender = item
    if not sender:
        sender = next((item for item in quoted if item and item != "REC UNREAD"
                       and item != "REC READ"), "")
    return sender, timestamp


def parse_cmgl(response: str) -> list[dict]:
    """AT+CMGL="ALL" -> [{index, status, from, timestamp, text}, ...]."""

    messages: list[dict] = []
    current: dict | None = None
    for raw in (response or "").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("+CMGL"):
            rest = line.split(":", 1)[1] if ":" in line else ""
            head = rest.split(",", 1)[0].strip()
            sender, timestamp = parse_header(line)
            status = re.search(r'"(REC\s+UNREAD|REC\s+READ|UNSENT|SENT|STOR UNREAD|UNKNOWN)"',
                               line, re.I)
            current = {"index": int(head) if head.isdigit() else None,
                       "status": status.group(1).upper().replace("  ", " ") if status else "",
                       "from": sender, "timestamp": timestamp, "text": ""}
            messages.append(current)
        elif upper in ("OK", "ERROR") or upper.startswith(("+CMS ERROR", "+CME ERROR")) \
                or upper.startswith("AT"):
            continue
        elif current is not None:
            current["text"] = (current["text"] + "\n" + line).strip()
    return messages


def parse_clcc(response: str) -> dict:
    """AT+CLCC -> {"state": ..., "number": ..., "direction": ...} for call 0."""

    for line in clean_lines(response):
        if not line.upper().startswith("+CLCC"):
            continue
        parts = [p.strip() for p in line.split(":", 1)[1].split(",")]
        if len(parts) < 3:
            continue
        states = {"0": "IDLE", "1": "OUTGOING", "2": "RINGING", "3": "ACTIVE",
                  "4": "HELD"}
        return {
            "state": states.get(parts[0], parts[0]),
            "direction": "IN" if parts[2] == "1" else "OUT",
            "number": parts[3].strip('"') if len(parts) > 3 else "",
        }
    return {}


#: Values after "<run>,<fix>" in the +CGNSINF payload of SIM808 R21 firmware.
#: Speed is reported in knots; the satellite counts are "in view" then "used".
CGNSINF_LAYOUT = ("year", "month", "day", "hour", "minute", "second",
                  "latitude", "longitude", "altitude", "speed_knots", "course",
                  "mode", "reserved1", "hdop", "pdop", "vdop", "reserved2",
                  "satellites_view", "satellites_used")

KNOTS_TO_KMH = 1.852


def _decimal(token: str) -> float | None:
    """Float when the token is a plain number, else None (blank, "A", ...)."""

    return float(token) if re.fullmatch(r"-?\d+(\.\d+)?", token or "") else None


def _cgnsinf_by_shape(body: list[str], out: dict) -> None:
    """Fill position for firmware that collapses the timestamp into one field."""

    decimals = [token for token in body if re.fullmatch(r"-?\d+\.\d+", token)]

    def precision(token: str) -> int:
        return len(token.split(".", 1)[1])

    for index in range(len(decimals) - 1):
        # the module prints coordinates with six decimals, time and DOP with fewer
        if precision(decimals[index]) < 4 or precision(decimals[index + 1]) < 4:
            continue
        latitude, longitude = float(decimals[index]), float(decimals[index + 1])
        if abs(latitude) <= 90.0 and abs(longitude) <= 180.0:
            out["latitude"], out["longitude"] = latitude, longitude
            break
    values = [value for value in (_decimal(token) for token in body[2:])
              if value is not None]
    for value in values:
        if out["altitude_m"] is None and -500 <= value <= 10000:
            out["altitude_m"] = value
    for value in values:
        if out["speed_kmh"] is None and 0 <= value <= 270:      # knots
            out["speed_kmh"] = round(value * KNOTS_TO_KMH, 2)
            break
    for token in body:
        if re.fullmatch(r"\d{2}", token) and out["satellites"] is None and token[0] in "12":
            out["satellites"] = int(token)


def parse_cgnsinf(response: str) -> dict:
    """AT+CGNSINF -> GPS fix dict, speed normalised to km/h.

    SIM808 R21 answers ``<run>,<fix>,<Y>,<M>,<D>,<h>,<m>,<s>,<lat>,<lon>,<alt>,
    <speed in knots>,<course>,<mode>,<reserved>,<HDOP>,<PDOP>,<VDOP>,<reserved>,
    <sats in view>,<sats used>,...``. Other builds merge the timestamp into one
    field and some reorder the tail, so a payload that does not start with a
    four digit year (or whose position is out of range) is read by shape instead.
    """

    value = last_value(response, "+CGNSINF")
    parts = [p.strip() for p in value.split(",")]
    out = {"powered": False, "valid": False, "latitude": None, "longitude": None,
           "altitude_m": None, "speed_kmh": None, "satellites": None}
    if len(parts) < 2:
        return out
    out["powered"] = parts[0] == "1"
    out["valid"] = parts[1] == "1"
    body = parts[2:]
    if len(body) >= len(CGNSINF_LAYOUT) and re.fullmatch(r"\d{4}", body[0] or ""):
        out["latitude"] = _decimal(body[6])
        out["longitude"] = _decimal(body[7])
        out["altitude_m"] = _decimal(body[8])
        knots = _decimal(body[9])
        out["speed_kmh"] = None if knots is None else round(knots * KNOTS_TO_KMH, 2)
        used, visible = _decimal(body[18]), _decimal(body[17])
        count = int(used if used is not None else (visible or 0))
        out["satellites"] = count or None
        if out["latitude"] is not None and abs(out["latitude"]) <= 90.0:
            return out
        out["latitude"] = out["longitude"] = None   # unexpected layout: read by shape
    _cgnsinf_by_shape(body, out)
    return out


def _to_int(value: str) -> int | None:
    match = re.search(r"-?\d+", str(value))
    return int(match.group()) if match else None
