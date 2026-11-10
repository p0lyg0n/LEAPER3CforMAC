#!/usr/bin/env python3
"""Generate Famicom-inspired EPROM IC iconset PNGs (stdlib only)."""

from __future__ import annotations

import os
import struct
import sys
import zlib


def encode_png(w: int, h: int, pixels: bytearray) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    raw = bytearray()
    stride = w * 4
    for y in range(h):
        raw.append(0)
        raw.extend(pixels[y * stride : (y + 1) * stride])
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


def render_icon(size: int) -> bytes:
    buf = bytearray(size * size * 4)
    # Palette: Famicom Cream (#F2EBD5), Crimson (#A61C24), Gold (#C59B38), Dark IC (#2B1B17)
    for y in range(size):
        ny = (y + 0.5) / size
        for x in range(size):
            nx = (x + 0.5) / size
            idx = (y * size + x) * 4

            margin = 0.08
            rad = 0.18
            dx = max(0.0, abs(nx - 0.5) - (0.5 - margin - rad))
            dy = max(0.0, abs(ny - 0.5) - (0.5 - margin - rad))
            dist = (dx * dx + dy * dy) ** 0.5 - rad
            if dist > 0.008:
                buf[idx : idx + 4] = b"\x00\x00\x00\x00"
                continue
            alpha = 255 if dist < -0.008 else int(max(0, min(255, (0.008 - dist) / 0.016 * 255)))

            r, g, b = 242, 235, 213
            if ny < 0.20 or ny > 0.86:
                r, g, b = 166, 28, 36
            if (0.20 <= ny < 0.225) or (0.835 <= ny <= 0.86):
                r, g, b = 197, 155, 56

            if 0.31 <= nx <= 0.69 and 0.28 <= ny <= 0.78:
                r, g, b = 43, 27, 23
                if ((nx - 0.5) ** 2 + (ny - 0.28) ** 2) ** 0.5 < 0.055:
                    r, g, b = 242, 235, 213
                elif 0.40 <= nx <= 0.60 and 0.42 <= ny <= 0.60:
                    r, g, b = 72, 118, 168
                    if 0.43 <= nx <= 0.57 and 0.45 <= ny <= 0.57:
                        r, g, b = 34, 48, 72

            for p in range(7):
                py0 = 0.31 + p * 0.068
                if py0 <= ny <= py0 + 0.036 and ((0.22 <= nx < 0.31) or (0.69 < nx <= 0.78)):
                    r, g, b = 197, 155, 56

            buf[idx] = r
            buf[idx + 1] = g
            buf[idx + 2] = b
            buf[idx + 3] = alpha
    return encode_png(size, size, buf)


def generate_iconset(out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    sizes = [
        (16, "icon_16x16.png"),
        (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"),
        (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"),
        (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"),
        (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"),
    ]
    cache: dict[int, bytes] = {}
    for sz, fname in sizes:
        if sz not in cache:
            cache[sz] = render_icon(sz)
        with open(os.path.join(out_dir, fname), "wb") as f:
            f.write(cache[sz])


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(f"Usage: {sys.argv[0]} <iconset_dir>")
    generate_iconset(sys.argv[1])

