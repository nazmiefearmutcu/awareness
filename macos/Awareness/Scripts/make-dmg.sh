#!/usr/bin/env bash
# Build Awareness.app (if needed) and wrap it in a drag-to-Applications DMG.
# Usage (from repo root or any cwd):
#   ./macos/Awareness/Scripts/make-dmg.sh
#   VERSION=0.2.1 RELEASE=1 ./macos/Awareness/Scripts/make-dmg.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"   # macos/Awareness
REPO="$(cd "${ROOT}/../.." && pwd)"         # repo root

APP_NAME="Awareness"
APP_SRC="${REPO}/dist/${APP_NAME}.app"
VERSION="${VERSION:-}"
if [[ -z "${VERSION}" ]]; then
  VERSION="$(
    /usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' \
      "${ROOT}/Resources/Info.plist" 2>/dev/null || echo "0.0.0"
  )"
fi

ARCH="$(uname -m)"
case "${ARCH}" in
  arm64) ARCH_LABEL="arm64" ;;
  x86_64) ARCH_LABEL="x86_64" ;;
  *) ARCH_LABEL="${ARCH}" ;;
esac

OUT_DIR="${REPO}/dist"
DMG_NAME="${APP_NAME}-${VERSION}-${ARCH_LABEL}.dmg"
DMG_PATH="${OUT_DIR}/${DMG_NAME}"
STAGE="${OUT_DIR}/dmg-stage"
VOL_NAME="Awareness ${VERSION}"

echo "==> Preparing ${APP_NAME}.app for DMG (v${VERSION}, ${ARCH_LABEL})…"

# Fresh release package (no machine-local AWARENESS_REPO pin).
export RELEASE="${RELEASE:-1}"
"${ROOT}/Scripts/build-app.sh"

if [[ ! -x "${APP_SRC}/Contents/MacOS/${APP_NAME}" ]]; then
  echo "error: ${APP_SRC} missing or not executable" >&2
  exit 1
fi

# Strip developer machine pin for redistribution.
rm -f "${APP_SRC}/Contents/Resources/AWARENESS_REPO.txt"
# Re-sign after resource change.
if command -v codesign >/dev/null 2>&1; then
  codesign --force --deep --sign - "${APP_SRC}" 2>/dev/null || true
fi

rm -rf "${STAGE}"
mkdir -p "${STAGE}"
cp -R "${APP_SRC}" "${STAGE}/${APP_NAME}.app"
ln -s /Applications "${STAGE}/Applications"

# Short install note for first-time users (opens in Finder as text).
cat > "${STAGE}/README.txt" <<EOF
Awareness ${VERSION} — native macOS app
========================================

Install
  1. Drag Awareness.app into Applications
  2. First open: right-click → Open (ad-hoc signed; Gatekeeper may warn once)
  3. Ensure the Python engine is installed and on this machine:

       git clone https://github.com/nazmiefearmutcu/awareness.git
       cd awareness
       uv sync   # or: python3 -m venv .venv && pip install -e .
       # optional convenience:
       ln -s "\$PWD" ~/awareness_dev

The app is a native shell: it auto-starts awareness-api on 127.0.0.1 and
loads the dashboard in a WKWebView. It looks for:

  - AWARENESS_API_BIN / awareness-api on PATH
  - ~/awareness_dev/.venv/bin/awareness-api
  - repo with pyproject.toml + .venv near the app

Requirements: macOS 14+, Apple Silicon (${ARCH_LABEL} build).
Logs: ~/Library/Logs/Awareness/api.log

https://github.com/nazmiefearmutcu/awareness
EOF

# Temporary read/write image → compress to UDZO.
TMP_DMG="${OUT_DIR}/.${DMG_NAME}.tmp.dmg"
rm -f "${TMP_DMG}" "${DMG_PATH}"

# Size headroom for .app + README (app is small; 64 MB is plenty).
hdiutil create \
  -volname "${VOL_NAME}" \
  -srcfolder "${STAGE}" \
  -ov \
  -fs HFS+ \
  -format UDRW \
  "${TMP_DMG}" >/dev/null

# Optional: open window layout is best-effort; skip AppleScript for headless safety.
hdiutil convert "${TMP_DMG}" -format UDZO -imagekey zlib-level=9 -o "${DMG_PATH}" >/dev/null
rm -f "${TMP_DMG}"
rm -rf "${STAGE}"

# Checksums for release notes.
SHA256="$(shasum -a 256 "${DMG_PATH}" | awk '{print $1}')"
SIZE="$(du -h "${DMG_PATH}" | awk '{print $1}')"

echo "OK: ${DMG_PATH}"
echo "    size:   ${SIZE}"
echo "    sha256: ${SHA256}"
echo "${SHA256}" > "${DMG_PATH}.sha256"
echo "${DMG_PATH}"
