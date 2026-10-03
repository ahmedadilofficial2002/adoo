import pathlib
import shutil

root = pathlib.Path(r"c:\ado\jarvis-voice-butler")
removed, kept = [], []

# 1) Regenerable caches: __pycache__ under vb/ only, and the root pytest cache.
targets = [p for p in (root / "vb").rglob("__pycache__") if p.is_dir()]
pytest_cache = root / ".pytest_cache"
if pytest_cache.is_dir():
    targets.append(pytest_cache)

for target in targets:
    try:
        shutil.rmtree(target)
        removed.append(str(target))
    except Exception as exc:  # never abort the whole sweep on one lock
        kept.append("%s -> %s" % (target, exc))

# 2) Stale runtime log (regenerated on next run; logs dir itself is kept).
log = root / "vb/logs/vb.log"
if log.exists():
    try:
        log.unlink()
        removed.append(str(log))
    except Exception as exc:
        kept.append("%s -> %s" % (log, exc))

# 3) Dead code: unreferenced re-export shim.
shim = root / "vb/voice/speech_to_text.py"
if shim.exists():
    try:
        shim.unlink()
        removed.append(str(shim))
    except Exception as exc:
        kept.append("%s -> %s" % (shim, exc))

# 4) Prove the legacy folders were not touched.
legacy = {
    "jarvis_new": (root / "jarvis_new").is_dir(),
    "agent-starter-flutter": (root / "agent-starter-flutter").is_dir(),
    "jarvis_new/src/agent.py": (root / "jarvis_new/src/agent.py").is_file(),
    "flutter/pubspec.yaml": (root / "agent-starter-flutter/pubspec.yaml").is_file(),
}

lines = ["REMOVED %d" % len(removed)]
lines += ["  " + r for r in removed]
lines += ["FAILED %d" % len(kept)]
lines += ["  " + k for k in kept]
lines.append("LEGACY INTACT: " + repr(legacy))
lines.append("legacy file count jarvis_new=%d flutter=%d" % (
    sum(1 for _ in (root / "jarvis_new").rglob("*") if _.is_file()),
    sum(1 for _ in (root / "agent-starter-flutter").rglob("*") if _.is_file()),
))
pathlib.Path(r"c:\ado\chk.txt").write_text("\n".join(lines), encoding="utf-8")
print("\n".join(lines))
