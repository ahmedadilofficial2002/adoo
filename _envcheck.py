import importlib
import json
import os
import pathlib
import sys

out = []
out.append("python " + sys.version.replace("\n", " "))
mods = ["flask", "jinja2", "serial", "yaml", "dotenv", "requests", "werkzeug", "cv2", "pyttsx3", "vosk"]
for m in mods:
    try:
        mod = importlib.import_module(m)
        out.append("OK   %-10s %s" % (m, getattr(mod, "__version__", "?")))
    except Exception as exc:
        out.append("MISS %-10s %s" % (m, type(exc).__name__))

root = pathlib.Path(r"c:\ado\jarvis-voice-butler")
checks = {
    ".pytest_cache": root / ".pytest_cache",
    "vb/logs/vb.log": root / "vb/logs/vb.log",
    "vb/voice/speech_to_text.py": root / "vb/voice/speech_to_text.py",
}
for label, p in checks.items():
    out.append("%-32s %s" % (label, "EXISTS" if p.exists() else "gone"))

pycache = [str(p) for p in root.rglob("__pycache__") if "node_modules" not in str(p)]
out.append("__pycache__ dirs remaining: %d" % len(pycache))
for p in pycache[:10]:
    out.append("   " + p)

pathlib.Path(r"c:\ado\chk.txt").write_text("\n".join(out), encoding="utf-8")
print("\n".join(out))
