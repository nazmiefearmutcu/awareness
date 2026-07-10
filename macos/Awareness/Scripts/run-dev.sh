#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"   # macos/Awareness
REPO="$(cd "$ROOT/../.." && pwd)"         # repo root

# Prefer a packaged app if present; otherwise swift run.
APP="${REPO}/dist/Awareness.app"
if [[ -x "${APP}/Contents/MacOS/Awareness" ]]; then
  echo "==> Opening ${APP}"
  open "${APP}"
  exit 0
fi

echo "==> Running Awareness via swift run (debug)…"
cd "${ROOT}"
exec swift run Awareness
