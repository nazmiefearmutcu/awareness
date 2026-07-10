#!/usr/bin/env bash
# Generate AppIcon.icns (radar-style monochrome mark matching the SPA brand).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${ROOT}/Resources"
ICONSET="${OUT_DIR}/AppIcon.iconset"
ICNS="${OUT_DIR}/AppIcon.icns"

mkdir -p "${ICONSET}"

# Render a 1024 master PNG via Swift/AppKit (no external deps).
python3 - <<'PY'
from pathlib import Path
import struct, zlib

# Minimal pure-Python PNG: dark rounded square + white concentric rings + sweep.
size = 1024
cx = cy = size // 2

def pixel(x, y):
    # distance from center
    dx = x - cx + 0.5
    dy = y - cy + 0.5
    r = (dx * dx + dy * dy) ** 0.5
    # rounded rect mask
    m = 96  # corner radius
    # distance outside rounded rect
    ax, ay = abs(dx), abs(dy)
    half = size / 2 - 8
    if ax > half - m and ay > half - m:
        cr = ((ax - (half - m)) ** 2 + (ay - (half - m)) ** 2) ** 0.5
        if cr > m:
            return (0, 0, 0, 0)
    elif ax > half or ay > half:
        return (0, 0, 0, 0)

    # background
    bg = 12
    # rings
    rings = [120, 220, 320, 420]
    alpha_ring = 0
    for i, rr in enumerate(rings):
        d = abs(r - rr)
        if d < 10:
            alpha_ring = max(alpha_ring, int(255 * (1 - d / 10) * (0.45 + 0.15 * i)))
    # center dot
    if r < 28:
        return (255, 255, 255, 255)
    # sweep line toward top-right
    import math
    ang = math.atan2(-dy, dx)  # 0 = east
    # line at ~-35 deg
    target = -math.pi / 5
    dang = abs((ang - target + math.pi) % (2 * math.pi) - math.pi)
    on_sweep = dang < 0.04 and 40 < r < 430
    if on_sweep:
        return (255, 255, 255, 230)
    if alpha_ring:
        return (255, 255, 255, alpha_ring)
    return (bg, bg, bg, 255)

rows = []
for y in range(size):
    row = bytearray([0])  # filter none
    for x in range(size):
        r, g, b, a = pixel(x, y)
        row.extend([r, g, b, a])
    rows.append(bytes(row))
raw = b"".join(rows)

def chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
out = Path("/tmp/awareness-icon-1024.png")
out.write_bytes(png)
print(out)
PY

MASTER=/tmp/awareness-icon-1024.png
if [[ ! -f "${MASTER}" ]]; then
  echo "icon master missing" >&2
  exit 1
fi

# iconutil sizes
for s in 16 32 128 256 512; do
  sips -z "$s" "$s" "${MASTER}" --out "${ICONSET}/icon_${s}x${s}.png" >/dev/null
  sips -z $((s * 2)) $((s * 2)) "${MASTER}" --out "${ICONSET}/icon_${s}x${s}@2x.png" >/dev/null
done

# 32@1x already covered; iconutil expects specific names:
# icon_16x16.png, icon_16x16@2x.png, icon_32x32.png, icon_32x32@2x.png,
# icon_128x128.png, icon_128x128@2x.png, icon_256x256.png, icon_256x256@2x.png,
# icon_512x512.png, icon_512x512@2x.png

rm -f "${ICNS}"
iconutil -c icns "${ICONSET}" -o "${ICNS}"
rm -rf "${ICONSET}"
echo "OK: ${ICNS}"
