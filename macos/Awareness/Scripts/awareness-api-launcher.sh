#!/usr/bin/env bash
# Launcher used by Awareness.app when the dedicated awareness-api binary is missing.
# Uses the project venv when present; falls back to ~/awareness_dev/.venv for deps
# while always preferring $AWARENESS_REPO/src on PYTHONPATH.
# Forwards TERM/INT so the API child dies with the app.
set -euo pipefail

REPO="${AWARENESS_REPO:-}"
if [[ -z "$REPO" ]]; then
  # Walk up from this script: Scripts → Awareness → macos → repo
  # Or from .app: Resources → Contents → … (no pyproject); prefer env.
  HERE="$(cd "$(dirname "$0")" && pwd)"
  if [[ -f "${HERE}/../../../../pyproject.toml" ]]; then
    REPO="$(cd "${HERE}/../../../.." && pwd)"
  elif [[ -f "${HERE}/../../pyproject.toml" ]]; then
    REPO="$(cd "${HERE}/../.." && pwd)"
  else
    REPO="$(cd "${HERE}/../../.." && pwd)"
  fi
fi

resolve_python() {
  local candidates=(
    "${AWARENESS_PYTHON:-}"
    "${REPO}/.venv/bin/python"
    "${HOME}/awareness_dev/.venv/bin/python"
    "${HOME}/.venv/bin/python"
  )
  # PATH lookup last
  if command -v python3 >/dev/null 2>&1; then
    candidates+=("$(command -v python3)")
  fi
  local c
  for c in "${candidates[@]}"; do
    [[ -z "$c" ]] && continue
    if [[ -x "$c" ]]; then
      echo "$c"
      return 0
    fi
  done
  return 1
}

# Prefer a real awareness-api console script when its venv matches REPO.
if [[ -x "${REPO}/.venv/bin/awareness-api" ]]; then
  export PYTHONPATH="${REPO}/src${PYTHONPATH:+:$PYTHONPATH}"
  cd "$REPO"
  exec "${REPO}/.venv/bin/awareness-api"
fi

PY="$(resolve_python)" || {
  echo "awareness-api-launcher: no python found (install deps in .venv)" >&2
  exit 127
}

export PYTHONPATH="${REPO}/src${PYTHONPATH:+:$PYTHONPATH}"
cd "$REPO"

parent_pid=$PPID

"$PY" -c "from awareness.api.server import run; run()" &
api_pid=$!

cleanup() {
  if kill -0 "$api_pid" 2>/dev/null; then
    kill -TERM "$api_pid" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "$api_pid" 2>/dev/null || break
      sleep 0.2
    done
    kill -KILL "$api_pid" 2>/dev/null || true
  fi
  wait "$api_pid" 2>/dev/null || true
}
trap cleanup EXIT TERM INT HUP

# If the parent app vanishes (Dock quit / SIGKILL), reap the API child.
while kill -0 "$parent_pid" 2>/dev/null; do
  if ! kill -0 "$api_pid" 2>/dev/null; then
    wait "$api_pid"
    exit $?
  fi
  sleep 1
done
cleanup
exit 0
