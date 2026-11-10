#!/bin/sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
WITH_STANDALONE=0
SKIP_INFO=0

for arg in "$@"; do
    case "$arg" in
        --with-standalone) WITH_STANDALONE=1 ;;
        --skip-info) SKIP_INFO=1 ;;
        -h|--help)
            echo "Usage: ./setup.sh [--with-standalone] [--skip-info]"
            echo "  One-command macOS setup: installs libusb, creates Python venv, installs pyusb,"
            echo "  links 'leaper3c' / 'minipro' commands, and creates '/Applications/LEAPER-3C Web UI.app'."
            exit 0
            ;;
        *)
            echo "Unknown option: $arg" >&2
            exit 2
            ;;
    esac
done

if [ "$(uname -s)" != "Darwin" ]; then
    echo "[ERROR] This setup script is designed for macOS." >&2
    exit 1
fi

echo "==> [1/5] Checking Homebrew & libusb..."
if ! command -v brew >/dev/null 2>&1; then
    echo "[ERROR] Homebrew が見つかりません。先に以下で Homebrew をインストールしてください:" >&2
    echo '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"' >&2
    exit 1
fi

if ! brew list --versions libusb >/dev/null 2>&1; then
    echo "Installing libusb via Homebrew..."
    brew install libusb
else
    echo "libusb already installed: $(brew list --versions libusb)"
fi

echo "==> [2/5] Setting up Python virtual environment & PyUSB..."
if [ ! -x "$SCRIPT_DIR/venv/bin/python3" ]; then
    python3 -m venv "$SCRIPT_DIR/venv"
fi
"$SCRIPT_DIR/venv/bin/python3" -m pip install --upgrade pip >/dev/null
"$SCRIPT_DIR/venv/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"

echo "==> [3/5] Installing CLI commands & FireMinipro bridge..."
"$SCRIPT_DIR/install_fireminipro_bridge.sh"

echo "==> [4/5] Installing '/Applications/LEAPER-3C Web UI.app'..."
if [ "$WITH_STANDALONE" -eq 1 ]; then
    "$SCRIPT_DIR/build_standalone_app.sh" --install
else
    "$SCRIPT_DIR/install_webui_app.sh"
fi

echo "==> [5/5] Verifying LEAPER-3C connection (optional)..."
if [ "$SKIP_INFO" -eq 0 ]; then
    if ! "$SCRIPT_DIR/leaper3c" info; then
        echo "[INFO] 本体が未接続でもセットアップは完了しています。USB と 12V 電源を接続後にお試しください。"
    fi
fi

echo ""
echo "Setup complete!"
echo "  - Web UI App : /Applications/LEAPER-3C Web UI.app (ダブルクリックで Web UI 起動)"
echo "  - CLI / Web  : leaper3c ui   または   leaper3c info"

