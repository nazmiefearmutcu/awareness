#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"   # macos/Awareness
REPO="$(cd "$ROOT/../.." && pwd)"         # repo root

APP_NAME="Awareness"
PRODUCT="Awareness"
APP_DIR="${REPO}/dist/${APP_NAME}.app"
CONTENTS="${APP_DIR}/Contents"
MACOS_DIR="${CONTENTS}/MacOS"
RESOURCES_DIR="${CONTENTS}/Resources"

echo "==> Building ${PRODUCT} (release)…"
cd "${ROOT}"
swift build -c release --product "${PRODUCT}"

BIN="$(swift build -c release --product "${PRODUCT}" --show-bin-path)/${PRODUCT}"
if [[ ! -f "${BIN}" ]]; then
  echo "error: built binary not found at ${BIN}" >&2
  exit 1
fi

echo "==> Packaging ${APP_DIR}…"
rm -rf "${APP_DIR}"
mkdir -p "${MACOS_DIR}" "${RESOURCES_DIR}"

cp "${BIN}" "${MACOS_DIR}/${APP_NAME}"
chmod +x "${MACOS_DIR}/${APP_NAME}"

cp "${ROOT}/Resources/Info.plist" "${CONTENTS}/Info.plist"
# Ship a signal-safe API launcher for when awareness-api is not on PATH.
if [[ -f "${ROOT}/Scripts/awareness-api-launcher.sh" ]]; then
  cp "${ROOT}/Scripts/awareness-api-launcher.sh" "${RESOURCES_DIR}/awareness-api-launcher.sh"
  chmod +x "${RESOURCES_DIR}/awareness-api-launcher.sh"
fi
echo -n "APPL????" > "${CONTENTS}/PkgInfo"

echo "OK: ${APP_DIR}"
