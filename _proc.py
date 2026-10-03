import importlib
import pathlib
import subprocess

lines = []
try:
    out = subprocess.run(
        ["wmic", "process", "where", "name like '%python%'", "get", "ProcessId,CommandLine"],
        capture_output=True, text=True, timeout=30,
    ).stdout
    hits = [l.strip() for l in out.splitlines() if "vb" in l.lower() or "main" in l.lower()]
    lines.append("python processes referencing vb/main: %d" % len(hits))
    lines += ["  " + h[:200] for h in hits[:12]]
except Exception as exc:
    lines.append("process probe failed: %s" % exc)

try:
    mod = importlib.import_module("flask")
    lines.append("flask OK %s" % getattr(mod, "__version__", "?"))
except Exception as exc:
    lines.append("flask MISS %s" % exc)

log = pathlib.Path(r"c:\ado\jarvis-voice-butler\vb\logs\vb.log")
lines.append("vb.log exists=%s size=%s" % (log.exists(), log.stat().st_size if log.exists() else "-"))
if log.exists():
    try:
        lines.append("vb.log tail: " + log.read_text(encoding="utf-8", errors="replace")[-500:])
    except Exception as exc:
        lines.append("vb.log unreadable: %s" % exc)

pathlib.Path(r"c:\ado\chk.txt").write_text("\n".join(lines), encoding="utf-8")
print("\n".join(lines))
