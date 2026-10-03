"""Strict NMEA-0183 reader for the SIM808 GPS output.

Only GGA (position) and RMC (position + speed) are needed here. Sentences with a
bad or missing checksum are rejected rather than trusted: a half-received line
must never be reported as a position fix.
"""

from __future__ import annotations

KNOTS_TO_KMH = 1.852


def checksum_ok(sentence: str) -> bool:
    """Verify the ``*HH`` checksum of a full NMEA sentence."""

    text = (sentence or "").strip()
    if not text.startswith("$") or "*" not in text:
        return False
    body, _, tail = text[1:].partition("*")
    if not body or len(tail) < 2:
        return False
    try:
        expected = int(tail[:2], 16)
    except ValueError:
        return False
    computed = 0
    for character in body:
        computed ^= ord(character)
    return computed == expected


def fields(sentence: str) -> list[str]:
    """Split a sentence into comma fields without the checksum tail."""

    text = (sentence or "").strip()
    if text.startswith("$"):
        text = text[1:]
    return text.split("*", 1)[0].split(",")


def to_decimal(raw: str, hemisphere: str) -> float | None:
    """Convert NMEA ``dddmm.mmmm`` + hemisphere into signed decimal degrees."""

    raw = (raw or "").strip()
    if "." not in raw:
        return None
    whole = raw.split(".", 1)[0].lstrip("-")
    if len(whole) < 3:
        return None
    try:
        degrees = float(whole[:-2])
        minutes = float(raw[len(whole) - 2:])
    except ValueError:
        return None
    value = degrees + minutes / 60.0
    hemi = (hemisphere or "").strip().upper()
    if hemi in ("S", "W"):
        value = -value
    elif hemi not in ("N", "E", ""):
        return None
    if raw.startswith("-") and hemi == "":
        value = -value
    return round(value, 6)


def _number(raw: str) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def parse_gga(parts: list[str]) -> dict:
    """$GNGGA,hhmmss.ss,lat,N,lon,E,quality,sats,hdop,alt,M,..."""

    out = {"type": "GGA", "valid": False, "latitude": None, "longitude": None,
           "altitude_m": None, "speed_kmh": None, "satellites": None, "utc": ""}
    if len(parts) < 7:
        return out
    quality = parts[6].strip()
    out["valid"] = quality not in ("", "0")
    out["fix_quality"] = _number(quality)
    out["latitude"] = to_decimal(parts[2], parts[3])
    out["longitude"] = to_decimal(parts[4], parts[5])
    if len(parts) > 7:
        satellites = _number(parts[7])
        out["satellites"] = int(satellites) if satellites is not None else None
    if len(parts) > 9:
        out["altitude_m"] = _number(parts[9])
    if len(parts) > 1:
        out["utc"] = parts[1]
    if out["latitude"] is None or out["longitude"] is None:
        out["valid"] = False
    return out


def parse_rmc(parts: list[str]) -> dict:
    """$GNRMC,hhmmss.ss,A,lat,N,lon,E,speed,course,ddmmyy,..."""

    out = {"type": "RMC", "valid": False, "latitude": None, "longitude": None,
           "altitude_m": None, "speed_kmh": None, "satellites": None, "utc": ""}
    if len(parts) < 7:
        return out
    out["valid"] = parts[2].strip().upper() == "A"
    out["utc"] = parts[1]
    out["latitude"] = to_decimal(parts[3], parts[4])
    out["longitude"] = to_decimal(parts[5], parts[6])
    knots = _number(parts[7]) if len(parts) > 7 else None
    if knots is not None:
        out["speed_kmh"] = round(knots * KNOTS_TO_KMH, 2)
    if len(parts) > 8:
        out["course_deg"] = _number(parts[8])
    if out["latitude"] is None or out["longitude"] is None:
        out["valid"] = False
    return out


def parse(sentence: str) -> dict | None:
    """Parse one sentence. None means "not usable", never a partial guess."""

    text = (sentence or "").strip()
    if not text.startswith("$") or not checksum_ok(text):
        return None
    parts = fields(text)
    talker_id = parts[0].upper()
    if talker_id.endswith("GGA"):
        return parse_gga(parts)
    if talker_id.endswith("RMC"):
        return parse_rmc(parts)
    return None


def merge(current: dict | None, reading: dict) -> dict:
    """Fold one sentence into the accumulated fix, keeping the newest values."""

    current = dict(current or {})
    for key in ("latitude", "longitude", "altitude_m", "speed_kmh", "satellites", "utc"):
        value = reading.get(key)
        if value is not None:
            current[key] = value
    current["valid"] = bool(reading.get("valid")) or bool(current.get("valid"))
    current["type"] = reading.get("type", current.get("type", ""))
    return current
