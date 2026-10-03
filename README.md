# ado / jarvis-voice-butler monorepo

Local-first voice/AI agent experiments. Three independent projects live under
`jarvis-voice-butler/`. Pick the one that matches what you want to run.

| Sub-folder | Kind | Entry | Requires |
|---|---|---|---|
| `vb/` | Python local desktop assistant (text REPL or local mic+TTS) | `python -m vb --text` | Python 3.11+, pip deps in `vb/requirements.txt`; optional Ollama, Vosk, SIM808 hardware |
| `jarvis_new/` | LiveKit + Gemini realtime web voice agent | backend: `uv run python src/agent.py start`; frontend: `cd frontend && pnpm dev` → `http://localhost:3000` | uv, Google AI Studio key, LiveKit Cloud keys, Node/pnpm, Playwright (Chromium) |
| `agent-starter-flutter/` | Flutter mobile UI template for connecting to a LiveKit agent voice room | `flutter run` | Flutter SDK + LiveKit agent already running elsewhere |

## Env / secrets rules (critical — keep pushes clean)

- **Never** commit `.env`, `.env.local`, `.env.*.local`, service-account
  JSONs, `*.pem`, `*.key`, Vosk model dirs, Ollama weights, or LiveKit API
  secrets directly. The root `.gitignore` blocks them.
- For each project: copy its `*.env.example` into a real `.env` / `.env.local`
  next to the example, and fill values locally.
- Keep `.env.example` files as `<your_*_here>` placeholders only.
  `jarvis_new/.env.example` was already scrubbed to placeholders.

## Root helpers

Loose Python scripts at the repo root (`_audit2.txt`, `_cleanup.py`,
`_compile.txt`, `_envcheck.py`, `_proc.py`, `chk.txt`) are local-ad-hoc
tooling; treat them as scratchpad helpers, not as part of any agent project.

## Clean push checklist

Before `git push`:

1. `git status` → clean (or only files you intentionally changed).
2. `git diff --cached | grep -i "api_key\|secret\|password\|AQ\.\|eyJ"` → empty (no secrets staged).
3. `git ls-files --stage | grep "^160000"` → empty (no submodule stubs; this repo is a single monorepo tree).
4. `git add -A && git commit -m "..." && git push` (force only if origin diverged on purpose).
