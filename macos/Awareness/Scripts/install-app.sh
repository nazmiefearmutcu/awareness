#!/usr/bin/env bash
# Build (if needed) and install Awareness.app to ~/Applications for `open -a Awareness`.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO="$(cd "${ROOT}/../.." && pwd)"
SRC="${REPO}/dist/Awareness.app"
DEST_DIR="${HOME}/Applications"
DEST="${DEST_DIR}/Awareness.app"

if [[ ! -x "${SRC}/Contents/MacOS/Awareness" ]]; then
  echo "==> Building first…"
  "${ROOT}/Scripts/build-app.sh"
fi

mkdir -p "${DEST_DIR}"
# Replace previous install
rm -rf "${DEST}"
cp -R "${SRC}" "${DEST}"
# Drop a convenience marker for CLI discovery
echo "${REPO}" > "${DEST}/Contents/Resources/AWARENESS_REPO.txt" 2>/dev/null || true

echo "Installed: ${DEST}"
echo "Launch:    open -a Awareness"
echo "Or:        awareness dashboard"
