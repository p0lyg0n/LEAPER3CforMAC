#!/bin/sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
APP_NAME="LEAPER-3C Web UI"
APP_BUNDLE="$SCRIPT_DIR/dist/$APP_NAME.app"
ARCH="$(uname -m)"
ZIP_OUT="$SCRIPT_DIR/dist/LEAPER-3C-WebUI-macos-${ARCH}.zip"
INSTALL_TO_APPS=0

for arg in "$@"; do
    case "$arg" in
        --install) INSTALL_TO_APPS=1 ;;
        -h|--help)
            echo "Usage: ./build_standalone_app.sh [--install]"
            echo "  Builds self-contained dist/$APP_NAME.app (with Python + pyusb + libusb) and zip archive."
            echo "  --install  Also installs the standalone .app into /Applications/"
            exit 0
            ;;
        *)
            echo "Unknown option: $arg" >&2
            exit 2
            ;;
    esac
done

BASE_PY=""
for cand in "$SCRIPT_DIR/venv/bin/python3" "$SCRIPT_DIR/.venv/bin/python3" "$(command -v python3 2>/dev/null || true)"; do
    if [ -n "$cand" ] && [ -x "$cand" ]; then
        BASE_PY="$cand"
        break
    fi
done
if [ -z "$BASE_PY" ]; then
    echo "[ERROR] python3 が見つかりません。" >&2
    exit 1
fi

LIBUSB_PATH=""
if command -v brew >/dev/null 2>&1; then
    LIBUSB_PREFIX="$(brew --prefix libusb 2>/dev/null || true)"
    if [ -n "$LIBUSB_PREFIX" ] && [ -f "$LIBUSB_PREFIX/lib/libusb-1.0.dylib" ]; then
        LIBUSB_PATH="$LIBUSB_PREFIX/lib/libusb-1.0.dylib"
    fi
fi
if [ -z "$LIBUSB_PATH" ]; then
    for cand in /opt/homebrew/opt/libusb/lib/libusb-1.0.dylib /opt/homebrew/lib/libusb-1.0.dylib /usr/local/opt/libusb/lib/libusb-1.0.dylib /usr/local/lib/libusb-1.0.dylib; do
        if [ -f "$cand" ]; then
            LIBUSB_PATH="$cand"
            break
        fi
    done
fi
if [ -z "$LIBUSB_PATH" ]; then
    echo "[ERROR] libusb-1.0.dylib が見つかりません。先に 'brew install libusb' を実行してください。" >&2
    exit 1
fi

# Resolve real file and stage as libusb-1.0.dylib so PyInstaller bundles the exact library binary
REAL_LIBUSB="$("$BASE_PY" -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$LIBUSB_PATH")"
echo "Using libusb: $REAL_LIBUSB"

if [ -d "/Library/Developer/CommandLineTools" ]; then
    export DEVELOPER_DIR="/Library/Developer/CommandLineTools"
    export PATH="/Library/Developer/CommandLineTools/usr/bin:$PATH"
fi

BUILD_VENV="$SCRIPT_DIR/.build_venv"
if [ ! -x "$BUILD_VENV/bin/pyinstaller" ]; then
    echo "Creating build venv and installing pyinstaller + requirements..."
    "$BASE_PY" -m venv "$BUILD_VENV"
    "$BUILD_VENV/bin/pip" install --upgrade pip
    "$BUILD_VENV/bin/pip" install -r "$SCRIPT_DIR/requirements.txt" pyinstaller
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
cp "$REAL_LIBUSB" "$TMP_DIR/libusb-1.0.dylib"
"$BUILD_VENV/bin/python3" "$SCRIPT_DIR/make_app_icon.py" "$TMP_DIR/AppIcon.iconset"
ICNS_PATH="$TMP_DIR/AppIcon.icns"
iconutil -c icns "$TMP_DIR/AppIcon.iconset" -o "$ICNS_PATH"

rm -rf "$SCRIPT_DIR/build" "$APP_BUNDLE" "$ZIP_OUT"

"$BUILD_VENV/bin/pyinstaller" \
    --noconfirm \
    --clean \
    --windowed \
    --onedir \
    --name "$APP_NAME" \
    --osx-bundle-identifier "io.github.p0lyg0n.leaper3c-webui" \
    --icon "$ICNS_PATH" \
    --add-binary "$TMP_DIR/libusb-1.0.dylib:." \
    --paths "$SCRIPT_DIR" \
    --hidden-import usb.backend.libusb1 \
    --hidden-import leaper3c \
    --hidden-import leaper3c_ui \
    --distpath "$SCRIPT_DIR/dist" \
    --workpath "$SCRIPT_DIR/build/pyinstaller" \
    --specpath "$TMP_DIR" \
    "$SCRIPT_DIR/webui_launcher_entry.py"

chmod -R u+w "$APP_BUNDLE"
xattr -cr "$APP_BUNDLE"
codesign --force --deep --sign - "$APP_BUNDLE"

ditto -c -k --sequesterRsrc --keepParent "$APP_BUNDLE" "$ZIP_OUT"
echo "Built standalone app : $APP_BUNDLE"
echo "Built release archive: $ZIP_OUT"

if [ "$INSTALL_TO_APPS" -eq 1 ]; then
    TARGET_APP="/Applications/$APP_NAME.app"
    rm -rf "$TARGET_APP"
    ditto "$APP_BUNDLE" "$TARGET_APP"
    chmod -R u+w "$TARGET_APP"
    xattr -cr "$TARGET_APP"
    codesign --force --deep --sign - "$TARGET_APP"
    LSREG="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
    if [ -x "$LSREG" ]; then
        "$LSREG" -f "$TARGET_APP" >/dev/null 2>&1 || true
    fi
    echo "Installed standalone app to: $TARGET_APP"
fi

