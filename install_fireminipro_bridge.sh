#!/bin/sh
set -eu
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

chmod +x "$SCRIPT_DIR/leaper3c" "$SCRIPT_DIR/leaper3c.py" "$SCRIPT_DIR/minipro" "$SCRIPT_DIR/minipro_shim.py" "$SCRIPT_DIR/leaper3c_ui.py"

if [ -d "/opt/homebrew/bin" ]; then
    ln -sf "$SCRIPT_DIR/minipro" /opt/homebrew/bin/minipro
    ln -sf "$SCRIPT_DIR/leaper3c" /opt/homebrew/bin/leaper3c
    echo "Linked /opt/homebrew/bin/minipro -> $SCRIPT_DIR/minipro"
    echo "Linked /opt/homebrew/bin/leaper3c -> $SCRIPT_DIR/leaper3c"
fi

APP="/Applications/fireminipro.app"
if [ -d "$APP" ]; then
    BIN="$APP/Contents/MacOS/minipro"
    ORIG="$APP/Contents/MacOS/minipro.orig"
    if [ -e "$BIN" ] && [ ! -L "$BIN" ] && [ ! -e "$ORIG" ]; then
        mv "$BIN" "$ORIG"
    fi
    ln -sf "$SCRIPT_DIR/minipro" "$BIN"
    chmod -R u+w "$APP"
    xattr -cr "$APP"
    codesign --force --deep --sign - "$APP"
    echo "Patched $APP to use LEAPER-3C minipro shim (original backed up at minipro.orig)"
fi

