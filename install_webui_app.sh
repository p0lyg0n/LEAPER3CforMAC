#!/bin/sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
APP_PATH="/Applications/LEAPER-3C Web UI.app"
CONTENTS="$APP_PATH/Contents"
MACOS_DIR="$CONTENTS/MacOS"
RES_DIR="$CONTENTS/Resources"

if [ -x "$SCRIPT_DIR/venv/bin/python3" ]; then
    PYTHON_BIN="$SCRIPT_DIR/venv/bin/python3"
elif [ -x "$SCRIPT_DIR/.venv/bin/python3" ]; then
    PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python3"
else
    PYTHON_BIN="$(command -v python3)"
fi

rm -rf "$APP_PATH"
mkdir -p "$MACOS_DIR" "$RES_DIR"

# Generate Famicom-inspired EPROM IC icon (.icns) using Python stdlib + macOS iconutil
TMP_ICONSET="$(mktemp -d)/AppIcon.iconset"
mkdir -p "$TMP_ICONSET"

"$PYTHON_BIN" - "$TMP_ICONSET" <<'PY'
import os
import struct
import sys
import zlib

out_dir = sys.argv[1]


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
    # Palette: Famicom Cream (#F2EBD5), Crimson (#A61C24), Gold (#C59B38), Dark IC (#241C1A)
    for y in range(size):
        ny = (y + 0.5) / size
        for x in range(size):
            nx = (x + 0.5) / size
            idx = (y * size + x) * 4

            # Rounded-square app icon silhouette
            margin = 0.08
            rad = 0.18
            dx = max(0.0, abs(nx - 0.5) - (0.5 - margin - rad))
            dy = max(0.0, abs(ny - 0.5) - (0.5 - margin - rad))
            dist = (dx * dx + dy * dy) ** 0.5 - rad
            if dist > 0.008:
                buf[idx : idx + 4] = b"\x00\x00\x00\x00"
                continue
            alpha = 255 if dist < -0.008 else int(max(0, min(255, (0.008 - dist) / 0.016 * 255)))

            # Base cream background + crimson top/bottom accent border
            r, g, b = 242, 235, 213  # #F2EBD5
            if ny < 0.20 or ny > 0.86:
                r, g, b = 166, 28, 36  # #A61C24
            if (0.20 <= ny < 0.225) or (0.835 <= ny <= 0.86):
                r, g, b = 197, 155, 56  # #C59B38

            # DIP IC package in center
            if 0.31 <= nx <= 0.69 and 0.28 <= ny <= 0.78:
                r, g, b = 43, 27, 23  # #2B1B17
                # Notch on top
                if ((nx - 0.5) ** 2 + (ny - 0.28) ** 2) ** 0.5 < 0.055:
                    r, g, b = 242, 235, 213
                # UV EPROM quartz window in middle
                elif 0.40 <= nx <= 0.60 and 0.42 <= ny <= 0.60:
                    r, g, b = 72, 118, 168
                    if 0.43 <= nx <= 0.57 and 0.45 <= ny <= 0.57:
                        r, g, b = 34, 48, 72

            # Gold DIP pins left & right (7 pairs)
            for p in range(7):
                py0 = 0.31 + p * 0.068
                if py0 <= ny <= py0 + 0.036 and (
                    (0.22 <= nx < 0.31) or (0.69 < nx <= 0.78)
                ):
                    r, g, b = 197, 155, 56

            buf[idx] = r
            buf[idx + 1] = g
            buf[idx + 2] = b
            buf[idx + 3] = alpha
    return encode_png(size, size, buf)


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
cache = {}
for sz, fname in sizes:
    if sz not in cache:
        cache[sz] = render_icon(sz)
    with open(os.path.join(out_dir, fname), "wb") as f:
        f.write(cache[sz])
PY

if command -v iconutil >/dev/null 2>&1; then
    iconutil -c icns "$TMP_ICONSET" -o "$RES_DIR/AppIcon.icns"
fi
rm -rf "$(dirname "$TMP_ICONSET")"

cat > "$CONTENTS/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>ja</string>
    <key>CFBundleExecutable</key>
    <string>leaper3c-webui</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundleIdentifier</key>
    <string>io.github.p0lyg0n.leaper3c-webui</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>LEAPER-3C Web UI</string>
    <key>CFBundleDisplayName</key>
    <string>LEAPER-3C Web UI</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>LSMinimumSystemVersion</key>
    <string>12.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
PLIST

cat > "$RES_DIR/launch.sh" <<EOF
#!/bin/sh
set -eu

REPO_DIR="$SCRIPT_DIR"
PYTHON_BIN="$PYTHON_BIN"
URL="http://127.0.0.1:8765/"
STATE_DIR="\$HOME/Library/Application Support/LEAPER3C"
LOG_FILE="\$HOME/Library/Logs/LEAPER3C-WebUI.log"
STAMP_FILE="\$STATE_DIR/webui.stamp"
mkdir -p "\$STATE_DIR" "\$(dirname "\$LOG_FILE")"

CUR_STAMP="\$("\$PYTHON_BIN" -c 'import hashlib, pathlib, sys; h=hashlib.sha256(); [h.update(pathlib.Path(p).read_bytes()) for p in sys.argv[1:]]; print(h.hexdigest()[:16])' "\$REPO_DIR/leaper3c.py" "\$REPO_DIR/leaper3c_ui.py")"

server_alive() {
    "\$PYTHON_BIN" -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8765/api/job", timeout=0.6)' >/dev/null 2>&1
}

if server_alive; then
    OLD_STAMP=""
    if [ -f "\$STAMP_FILE" ]; then
        OLD_STAMP="\$(cat "\$STAMP_FILE" 2>/dev/null || true)"
    fi
    if [ "\$OLD_STAMP" = "\$CUR_STAMP" ]; then
        open "\$URL"
        exit 0
    fi
    pkill -f 'leaper3c\.py ui' >/dev/null 2>&1 || true
    sleep 0.4
fi

printf '%s\n' "\$CUR_STAMP" > "\$STAMP_FILE"
nohup "\$PYTHON_BIN" "\$REPO_DIR/leaper3c.py" ui --no-browser >> "\$LOG_FILE" 2>&1 </dev/null &

i=0
while [ "\$i" -lt 40 ]; do
    if server_alive; then
        open "\$URL"
        exit 0
    fi
    sleep 0.15
    i=\$((i + 1))
done

osascript -e 'display alert "LEAPER-3C Web UI の起動に失敗しました" message "ログを確認してください: ~/Library/Logs/LEAPER3C-WebUI.log" as critical' >/dev/null 2>&1 || true
exit 1
EOF
chmod +x "$RES_DIR/launch.sh"

# LaunchServices requires a real Mach-O executable in Contents/MacOS for double-click / open -a
CLANG_BIN="/Library/Developer/CommandLineTools/usr/bin/clang"
if [ ! -x "$CLANG_BIN" ]; then
    CLANG_BIN="$(command -v clang 2>/dev/null || true)"
fi

if [ -n "$CLANG_BIN" ] && [ -x "$CLANG_BIN" ]; then
    TMP_C="$(mktemp /tmp/leaper3c_launcher.XXXXXX.c)"
    cat > "$TMP_C" <<C_EOF
#include <limits.h>
#include <libgen.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

int main(void) {
    char exe[PATH_MAX];
    uint32_t sz = sizeof(exe);
    if (_NSGetExecutablePath(exe, &sz) != 0) return 1;
    char *macos_dir = dirname(exe);
    char script[PATH_MAX];
    snprintf(script, sizeof(script), "%s/../Resources/launch.sh", macos_dir);
    char *argv[] = {"/bin/sh", script, NULL};
    execv("/bin/sh", argv);
    return 1;
}
C_EOF
    SDK_FLAG=""
    if [ -d "/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk" ]; then
        SDK_FLAG="-isysroot /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk"
    fi
    "$CLANG_BIN" -O2 $SDK_FLAG -o "$MACOS_DIR/leaper3c-webui" "$TMP_C"
    rm -f "$TMP_C"
else
    # Fallback wrapper if clang is unavailable
    ln -sf "../Resources/launch.sh" "$MACOS_DIR/leaper3c-webui"
fi

chmod +x "$MACOS_DIR/leaper3c-webui"
chmod -R u+w "$APP_PATH"
xattr -cr "$APP_PATH"
codesign --force --deep --sign - "$APP_PATH"
LSREG="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
if [ -x "$LSREG" ]; then
    "$LSREG" -f "$APP_PATH" >/dev/null 2>&1 || true
fi

echo "Installed $APP_PATH (points to $SCRIPT_DIR)"
