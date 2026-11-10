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

# Generate Famicom-inspired EPROM IC icon (.icns) using shared helper + iconutil
TMP_ICON_PARENT="$(mktemp -d)"
TMP_ICONSET="$TMP_ICON_PARENT/AppIcon.iconset"
"$PYTHON_BIN" "$SCRIPT_DIR/make_app_icon.py" "$TMP_ICONSET"
if command -v iconutil >/dev/null 2>&1; then
    iconutil -c icns "$TMP_ICONSET" -o "$RES_DIR/AppIcon.icns"
fi
rm -rf "$TMP_ICON_PARENT"

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

show_err() {
    MSG="\$1"
    SAFE_MSG="\$(printf '%s' "\$MSG" | tr '\\"' " '")"
    osascript -e "display alert \"LEAPER-3C Web UI の起動に失敗しました\" message \"\$SAFE_MSG\" as critical" >/dev/null 2>&1 || true
    exit 1
}

if [ ! -x "\$PYTHON_BIN" ] || [ ! -f "\$REPO_DIR/leaper3c.py" ]; then
    show_err "必要なファイルが見つかりません (\$REPO_DIR)。フォルダを移動した場合は ./setup.sh を再実行してください。"
fi

CUR_STAMP="\$("\$PYTHON_BIN" -c 'import hashlib, pathlib, sys; h=hashlib.sha256(); [h.update(pathlib.Path(p).read_bytes()) for p in sys.argv[1:]]; print(h.hexdigest()[:16])' "\$REPO_DIR/leaper3c.py" "\$REPO_DIR/leaper3c_ui.py")" || show_err "ファイルチェックに失敗しました。./setup.sh を再実行してください。"

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

TAIL_LOG="\$(tail -n 10 "\$LOG_FILE" 2>/dev/null || true)"
show_err "6秒以内に Web UI が起動しませんでした。\$TAIL_LOG (ログ: ~/Library/Logs/LEAPER3C-WebUI.log)"
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
