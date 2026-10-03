"""Per-subsystem logging with privacy redaction.

Every module logs under the "vb.<subsystem>" namespace so each one can be tuned
independently. A redaction filter sits on the handlers so SIM numbers, IMSI/IMEI
digits and secrets cannot leak into vb.log unless redaction is switched off.
"""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_SECRET_KEYS = ("api_key", "apikey", "token", "password", "secret", "authorization")

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # key=value / "key": "value" style secrets
    (re.compile(r"(?i)\b(" + "|".join(_SECRET_KEYS) + r")\b(\s*[:=]\s*)(\"?)[^\s,;\"']+\3"), r"\1\2***"),
    # bearer tokens
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-]{8,}"), r"\1***"),
    # IMSI / IMEI: 15-17 consecutive digits
    (re.compile(r"(?<!\d)\d{15,17}(?!\d)"), "***IMEI/IMSI***"),
    # international phone numbers kept partially readable
    (re.compile(r"(?<!\d)(\+\d{1,3})?(\d{3})\d{5,9}(?!\d)"), r"\1\2***"),
]


class RedactingFilter(logging.Filter):
    """Mask secrets and subscriber identifiers before a record is emitted."""

    def __init__(self, enabled: bool = True) -> None:
        super().__init__()
        self.enabled = enabled

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.enabled:
            return True
        try:
            message = record.getMessage()
        except Exception:  # a broken %args should not lose the whole record
            return True
        redacted = message
        for pattern, replacement in _PATTERNS:
            redacted = pattern.sub(replacement, redacted)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


redact_filter = RedactingFilter(enabled=True)


def setup_logging(
    level: str = "INFO",
    log_dir: Path | str | None = None,
    *,
    file_name: str = "vb.log",
    max_bytes: int = 1_000_000,
    backups: int = 3,
    console: bool = True,
    redact: bool = True,
    quiet_libraries: tuple[str, ...] = ("comtypes", "urllib3", "requests", "werkzeug", "PIL"),
) -> Path | None:
    """Configure VB's logging once. Returns the log file path, or None if unwritable.

    Safe to call twice: existing handlers are replaced rather than duplicated.
    """

    redact_filter.enabled = redact
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        stream.addFilter(redact_filter)
        root.addHandler(stream)

    log_path: Path | None = None
    if log_dir:
        try:
            directory = Path(log_dir).expanduser()
            directory.mkdir(parents=True, exist_ok=True)
            log_path = directory / file_name
            file_handler = RotatingFileHandler(
                log_path, maxBytes=int(max_bytes), backupCount=int(backups), encoding="utf-8"
            )
            file_handler.setFormatter(fmt)
            file_handler.addFilter(redact_filter)
            root.addHandler(file_handler)
        except OSError as exc:
            # A read-only or full disk must not stop VB from starting.
            log_path = None
            logging.getLogger("vb.core.logging").warning("file logging disabled: %s", exc)

    for name in quiet_libraries:
        logging.getLogger(name).setLevel(logging.WARNING)
    # pyttsx3/comtypes are chatty while Windows SAPI initialises.
    logging.getLogger("comtypes.client._generate").setLevel(logging.ERROR)
    return log_path


def subsystem_logger(name: str) -> logging.Logger:
    """Logger for one subsystem, e.g. subsystem_logger("dashboard") -> vb.dashboard."""

    return logging.getLogger(f"vb.{name}")


def apply_subsystem_levels(levels: dict[str, Any]) -> None:
    """Apply {"sim808": "DEBUG", "dashboard": "WARNING"} from settings."""

    for name, value in (levels or {}).items():
        logging.getLogger(f"vb.{name}").setLevel(getattr(logging, str(value).upper(), logging.INFO))
