#!/usr/bin/env python3
"""
Standalone macOS .app entrypoint for LEAPER-3C Web UI.

Two execution modes inside a single bundled executable:
- Default (double-click): ensures the local HTTP server on 127.0.0.1:8765 is
  running in the background, opens the default browser, and shows a native
  macOS alert with the exact error details if startup fails.
- `--serve`: runs the HTTP server (`leaper3c_ui.serve_ui`).
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import leaper3c_ui

DEFAULT_PORT = 8765


def log_path() -> Path:
    p = Path.home() / "Library" / "Logs" / "LEAPER3C-WebUI.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def show_alert(title: str, message: str) -> None:
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    safe_msg = message.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display alert "{safe_title}" message "{safe_msg}" as critical'
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=30)
    except Exception:
        print(f"{title}: {message}", file=sys.stderr)


def server_alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/job", timeout=0.6) as resp:
            return resp.status == 200
    except Exception:
        return False


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def read_log_tail(path: Path, start_offset: int, max_chars: int = 900) -> str:
    try:
        data = path.read_bytes()
        chunk = data[start_offset:] if len(data) >= start_offset else data[-max_chars:]
        text = chunk.decode("utf-8", errors="replace").strip()
        if not text:
            text = data.decode("utf-8", errors="replace").strip()
        if len(text) > max_chars:
            text = "...\n" + text[-max_chars:]
        return text
    except Exception:
        return ""


def open_browser(url: str) -> None:
    try:
        subprocess.run(["open", url], check=False)
    except Exception:
        import webbrowser

        webbrowser.open(url)


def launch_and_open(port: int) -> int:
    url = f"http://127.0.0.1:{port}/"
    if server_alive(port):
        open_browser(url)
        return 0

    if port_in_use(port):
        show_alert(
            "LEAPER-3C Web UI を起動できません",
            f"ポート {port} は別のアプリが使用中です。\n既存の Web UI や他のツールを終了してから再度お試しください。",
        )
        return 1

    lp = log_path()
    start_offset = lp.stat().st_size if lp.exists() else 0

    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--serve", "--port", str(port)]
    else:
        cmd = [sys.executable, str(Path(__file__).resolve()), "--serve", "--port", str(port)]

    try:
        log_fh = lp.open("a", encoding="utf-8")
        log_fh.write(f"\n=== LEAPER-3C Web UI launch at {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        log_fh.flush()
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
        log_fh.close()
    except Exception as exc:
        show_alert("LEAPER-3C Web UI の起動に失敗しました", f"プロセス起動エラー: {exc}")
        return 1

    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if server_alive(port):
            open_browser(url)
            return 0
        rc = proc.poll()
        if rc is not None:
            tail = read_log_tail(lp, start_offset)
            detail = f"サーバーが終了しました (code={rc})。\n\n{tail}\n\nログ: {lp}".strip()
            show_alert("LEAPER-3C Web UI の起動に失敗しました", detail)
            return 1
        time.sleep(0.15)

    tail = read_log_tail(lp, start_offset)
    detail = f"8秒以内に Web UI が応答しませんでした。\n\n{tail}\n\nログ: {lp}".strip()
    show_alert("LEAPER-3C Web UI の起動に失敗しました", detail)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LEAPER-3C Web UI Standalone Launcher")
    parser.add_argument("--serve", action="store_true", help="Run the HTTP server directly")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="HTTP server port")
    args, _unknown = parser.parse_known_args(argv)

    if args.serve:
        return leaper3c_ui.serve_ui(port=args.port, open_browser=False)
    return launch_and_open(port=args.port)


if __name__ == "__main__":
    raise SystemExit(main())

