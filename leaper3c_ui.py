#!/usr/bin/env python3
"""
LEAPER-3C Local Web UI Server (`./leaper3c ui`)

Zero extra dependencies (uses Python stdlib http.server + threading).
Features:
- Live programmer & 260-IC catalog inspection
- Dynamic 28-pin / 32-pin Slave Textool SVG placement guide
- iNES .nes automatic PRG / CHR splitter & mirror padding
- Hex / ASCII buffer preview & .bin download
- Blank Check, Read, Erase, Program, and Verify with live progress
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import webbrowser
import zlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

import leaper3c


class UiState:
    """Thread-safe controller bridging the HTTP UI and LEAPER-3C hardware."""

    def __init__(self, backend_factory: Callable[[], Any] = leaper3c.PyUsbBackend) -> None:
        self.backend_factory = backend_factory
        self.usb_lock = threading.Lock()
        self.job_lock = threading.Lock()
        self.last_buffer: bytes = b""
        self.last_filename: str = "readback.bin"
        self.job: dict[str, Any] = {
            "id": 0,
            "running": False,
            "action": "",
            "phase": "Idle",
            "done": 0,
            "total": 0,
            "pct": 0,
            "ok": None,
            "error": None,
            "logs": [],
            "result": None,
        }

    def _log(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        with self.job_lock:
            self.job["logs"].append(f"[{ts}] {msg}")
            self.job["logs"] = self.job["logs"][-200:]

    def get_status(self) -> dict[str, Any]:
        with self.usb_lock:
            try:
                backend = self.backend_factory()
            except leaper3c.LeaperError as exc:
                return {"connected": False, "error": str(exc)}
            try:
                lp = leaper3c.Leaper3C(backend)
                info = lp.get_info()
                st = lp.read_status(timeout=5000)
                chips = lp.list_chips()
                loc = getattr(backend, "usb_location", "mock")
                return {
                    "connected": True,
                    "location": loc,
                    "banner": info.banner,
                    "firmware": info.fw_version,
                    "table_base": f"0x{info.table_base_addr:04X}",
                    "chip_count": len(chips),
                    "status_hex": st.hex(" "),
                }
            except Exception as exc:
                return {"connected": False, "error": str(exc)}
            finally:
                close_fn = getattr(backend, "close", None)
                if close_fn:
                    close_fn()

    def get_chips(self) -> list[dict[str, Any]]:
        with self.usb_lock:
            backend = self.backend_factory()
            try:
                lp = leaper3c.Leaper3C(backend)
                chips = lp.list_chips()
                return [
                    {
                        "vendor_id": c.vendor_id,
                        "vendor": c.vendor_name,
                        "dev_id": c.dev_id,
                        "name": c.name,
                        "label": f"{c.name} @ {c.vendor_name}",
                        "is_uv_eprom": c.is_uv_eprom,
                    }
                    for c in chips
                ]
            finally:
                close_fn = getattr(backend, "close", None)
                if close_fn:
                    close_fn()

    def get_chip_info(self, chip_name: str, vendor_name: str) -> dict[str, Any]:
        with self.usb_lock:
            backend = self.backend_factory()
            try:
                lp = leaper3c.Leaper3C(backend)
                chip = lp.find_chip(chip_name, vendor_name or None)
                params = lp.select_chip(chip)
                pin_count = 28 if any(k in chip.name for k in ("27C64", "27C128", "27C256", "27C512", "28C256")) else 32
                return {
                    "vendor": chip.vendor_name,
                    "vendor_id": chip.vendor_id,
                    "name": chip.name,
                    "dev_id": chip.dev_id,
                    "size_bytes": params.size_bytes,
                    "blocks_512": params.blocks_512,
                    "is_uv_eprom": chip.is_uv_eprom,
                    "pin_count": pin_count,
                    "vcc": f"{params.vcc_raw / 10:.1f}V",
                    "vpp": f"{params.vpp_raw / 10:.1f}V",
                }
            finally:
                close_fn = getattr(backend, "close", None)
                if close_fn:
                    close_fn()

    def inspect_uploaded_file(self, filename: str, data: bytes) -> dict[str, Any]:
        if len(data) >= 16 and data[:4] == b"NES\x1a":
            prg_banks = data[4]
            chr_banks = data[5]
            flags6 = data[6]
            flags7 = data[7]
            if (flags7 & 0x0C) == 0x08:
                prg_banks |= (data[9] & 0x0F) << 8
                chr_banks |= ((data[9] >> 4) & 0x0F) << 8
            mapper = ((flags6 >> 4) & 0x0F) | (flags7 & 0xF0)
            offset = 16 + (512 if (flags6 & 0x04) else 0)
            prg_size = prg_banks * 16384
            chr_size = chr_banks * 8192
            if len(data) < offset + prg_size + chr_size:
                raise leaper3c.LeaperError(".nes ファイルサイズがヘッダ定義より不足しています。")
            prg = data[offset : offset + prg_size]
            chr_data = data[offset + prg_size : offset + prg_size + chr_size]
            parts = [
                {
                    "kind": "PRG",
                    "name": f"{filename.rsplit('.', 1)[0]}.prg.bin",
                    "size": len(prg),
                    "crc32": f"0x{zlib.crc32(prg) & 0xFFFFFFFF:08X}",
                    "sum16": f"0x{leaper3c.sum16(prg):08X}",
                    "sha256": hashlib.sha256(prg).hexdigest(),
                    "b64": base64.b64encode(prg).decode("ascii"),
                    "hex_preview": format_hex_preview(prg, 512),
                }
            ]
            if chr_size > 0:
                parts.append(
                    {
                        "kind": "CHR",
                        "name": f"{filename.rsplit('.', 1)[0]}.chr.bin",
                        "size": len(chr_data),
                        "crc32": f"0x{zlib.crc32(chr_data) & 0xFFFFFFFF:08X}",
                        "sum16": f"0x{leaper3c.sum16(chr_data):08X}",
                        "sha256": hashlib.sha256(chr_data).hexdigest(),
                        "b64": base64.b64encode(chr_data).decode("ascii"),
                        "hex_preview": format_hex_preview(chr_data, 512),
                    }
                )
            return {"is_nes": True, "mapper": mapper, "parts": parts}

        return {
            "is_nes": False,
            "name": filename,
            "size": len(data),
            "crc32": f"0x{zlib.crc32(data) & 0xFFFFFFFF:08X}",
            "sum16": f"0x{leaper3c.sum16(data):08X}",
            "sha256": hashlib.sha256(data).hexdigest(),
            "b64": base64.b64encode(data).decode("ascii"),
            "hex_preview": format_hex_preview(data, 512),
        }

    def start_action(self, payload: dict[str, Any]) -> int:
        with self.job_lock:
            if self.job["running"]:
                raise leaper3c.LeaperError("別の処理が実行中です。完了までお待ちください。")
            job_id = int(self.job["id"]) + 1
            self.job.update(
                {
                    "id": job_id,
                    "running": True,
                    "action": str(payload.get("action", "")),
                    "phase": "Starting",
                    "done": 0,
                    "total": 0,
                    "pct": 0,
                    "ok": None,
                    "error": None,
                    "result": None,
                }
            )

        t = threading.Thread(target=self._run_action_worker, args=(job_id, payload), daemon=True)
        t.start()
        return job_id

    def _update_progress(self, phase: str, done: int, total: int) -> None:
        pct = int(done * 100 / total) if total else 100
        with self.job_lock:
            self.job["phase"] = phase
            self.job["done"] = done
            self.job["total"] = total
            self.job["pct"] = pct

    def _run_action_worker(self, job_id: int, payload: dict[str, Any]) -> None:
        action = str(payload.get("action", ""))
        chip_name = str(payload.get("chip", ""))
        vendor = str(payload.get("vendor", ""))
        pad_mode = str(payload.get("pad", "none"))
        do_erase = bool(payload.get("erase", False))
        do_verify = bool(payload.get("verify", True))
        b64_data = str(payload.get("data_b64", ""))
        raw = base64.b64decode(b64_data) if b64_data else b""

        try:
            with self.usb_lock:
                backend = self.backend_factory()
                try:
                    lp = leaper3c.Leaper3C(backend)
                    chip = lp.find_chip(chip_name, vendor or None)
                    params = lp.select_chip(chip)
                    self._log(f"Start {action}: {chip.vendor_name} {chip.name} ({params.size_bytes} bytes)")

                    res: dict[str, Any] = {}
                    if action == "blank-check":
                        self._update_progress("ブランクチェック中...", 0, 1)
                        st = lp.blank_check(chip)
                        self._update_progress("ブランクチェック完了", 1, 1)
                        self._log(f"ブランクチェック 正常 (status={st.hex(' ')})")
                        res = {"message": "ブランクチェック PASS（全 0xFF）", "status": st.hex(" ")}

                    elif action == "erase":
                        self._update_progress("デバイス消去中...", 0, 1)
                        st = lp.erase(chip)
                        self._update_progress("消去完了", 1, 1)
                        self._log(f"デバイス消去 正常 (status={st.hex(' ')})")
                        res = {"message": "消去 (イレース) PASS", "status": st.hex(" ")}

                    elif action == "read":
                        data = lp.read_chip(chip, progress_cb=lambda d, tot: self._update_progress("読出し中", d, tot))
                        self.last_buffer = data
                        self.last_filename = f"{chip.name.lower()}_readback.bin"
                        crc = f"0x{zlib.crc32(data) & 0xFFFFFFFF:08X}"
                        s16 = f"0x{leaper3c.sum16(data):08X}"
                        sha = hashlib.sha256(data).hexdigest()
                        self._log(f"読出し完了: {len(data)} bytes CRC32={crc}")
                        res = {
                            "message": f"読出し PASS ({len(data)} バイト)",
                            "size": len(data),
                            "crc32": crc,
                            "sum16": s16,
                            "sha256": sha,
                            "hex_preview": format_hex_preview(data, 1024),
                            "download_ready": True,
                        }

                    elif action == "verify":
                        if not raw:
                            raise leaper3c.LeaperError("照合するファイルデータがありません。")
                        expected = leaper3c.pad_image_to_chip(raw, params.size_bytes, pad_mode)
                        actual = lp.read_chip(chip, progress_cb=lambda d, tot: self._update_progress("ベリファイ読出し中", d, tot))
                        mm = leaper3c.verify_buffers(expected, actual)
                        if mm is not None:
                            off, exp_b, act_b = mm
                            raise leaper3c.LeaperError(
                                f"不一致 0x{off:05X}: 期待値=0x{exp_b:02X}, 実読値=0x{act_b:02X}"
                            )
                        crc = f"0x{zlib.crc32(actual) & 0xFFFFFFFF:08X}"
                        self._log(f"ベリファイ正常: 100% 一致 CRC32={crc}")
                        res = {"message": f"ベリファイ PASS (100% 一致 / CRC32={crc})", "crc32": crc}

                    elif action == "program":
                        if not raw:
                            raise leaper3c.LeaperError("書き込むファイルデータがありません。")
                        image = leaper3c.pad_image_to_chip(raw, params.size_bytes, pad_mode)
                        if do_erase and not chip.is_uv_eprom:
                            self._update_progress("事前消去中...", 0, 1)
                            lp.erase(chip)
                            self._log("事前消去 正常")

                        self._update_progress("事前ブランクチェック中...", 0, 1)
                        lp.blank_check(chip)
                        self._log("事前ブランクチェック 正常")

                        t0 = time.time()
                        lp.program_chip(chip, image, progress_cb=lambda d, tot: self._update_progress("書込み中", d, tot))
                        elapsed = time.time() - t0
                        self._log(f"書込み完了 ({elapsed:.2f}秒)")

                        if do_verify:
                            actual = lp.read_chip(chip, progress_cb=lambda d, tot: self._update_progress("ベリファイ照合中", d, tot))
                            mm = leaper3c.verify_buffers(image, actual)
                            if mm is not None:
                                off, exp_b, act_b = mm
                                raise leaper3c.LeaperError(
                                    f"ベリファイ不一致 0x{off:05X}: 期待値=0x{exp_b:02X}, 実読値=0x{act_b:02X}"
                                )
                            self._log("ベリファイ 正常 (100% 一致)")

                        crc = f"0x{zlib.crc32(image) & 0xFFFFFFFF:08X}"
                        res = {
                            "message": f"書込み{'＆ベリファイ' if do_verify else ''} PASS ({elapsed:.2f}秒, CRC32={crc})",
                            "crc32": crc,
                            "elapsed": round(elapsed, 2),
                        }
                    else:
                        raise leaper3c.LeaperError(f"未対応のアクション: {action}")

                    with self.job_lock:
                        if self.job["id"] == job_id:
                            self.job["running"] = False
                            self.job["ok"] = True
                            self.job["pct"] = 100
                            self.job["phase"] = "完了"
                            self.job["result"] = res
                finally:
                    close_fn = getattr(backend, "close", None)
                    if close_fn:
                        close_fn()
        except Exception as exc:
            self._log(f"ERROR: {exc}")
            with self.job_lock:
                if self.job["id"] == job_id:
                    self.job["running"] = False
                    self.job["ok"] = False
                    self.job["phase"] = "Failed"
                    self.job["error"] = str(exc)


def format_hex_preview(data: bytes, max_bytes: int = 512) -> str:
    chunk = data[:max_bytes]
    lines: list[str] = []
    for off in range(0, len(chunk), 16):
        row = chunk[off : off + 16]
        hexs = " ".join(f"{b:02X}" for b in row)
        asciis = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        lines.append(f"{off:06X}  {hexs:<47}  |{asciis}|")
    if len(data) > max_bytes:
        lines.append(f"... ({len(data) - max_bytes} more bytes; total {len(data)} bytes)")
    return "\n".join(lines)


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>LEAPER-3C macOS Programmer UI</title>
<style>
  :root, :root[data-theme="famicom"] {
    --bg: #F2EBD5; --panel: #FAF5E4; --header-bg: #A61C24; --header-text: #FAF5E4;
    --header-border: #C59B38; --border: #D2C3A1; --text: #2B1E1A; --muted: #6E5B4B;
    --accent: #A61C24; --accent-hover: #8C151C; --accent-text: #FAF5E4;
    --input-bg: #FFFDF5; --btn-bg: #EADFC4; --btn-hover: #DFD0AE; --btn-border: #C4B18B;
    --danger-bg: #B91C1C; --danger-hover: #991B1B; --danger-border: #7F1D1D; --danger-text: #FFFFFF;
    --pre-bg: #F7F1DD; --pre-text: #2B1E1A; --drop-bg: #F5EED8;
    --warn-bg: #FDF3D7; --warn-border: #C59B38; --warn-text: #5B3E10;
    --badge-bg: #8C151C; --badge-text: #FAF5E4; --badge-border: #C59B38;
    --badge-ok-bg: #276749; --badge-ok-text: #F0FFF4; --badge-ok-border: #48BB78;
    --badge-err-bg: #742A2A; --badge-err-text: #FFF5F5; --badge-err-border: #F56565;
    --prog-grad: linear-gradient(90deg, #A61C24, #C59B38);
    --svg-body: #3A2A24; --svg-ic: #2B1E1A; --svg-pin-on: #C59B38; --svg-pin-off: #8C7A62;
    --svg-txt: #2B1E1A; --svg-sub: #C59B38; --svg-ic-txt: #FAF5E4; --svg-ic-sub: #F6D889;
  }
  :root[data-theme="nes"] {
    --bg: #D0D0D4; --panel: #E2E2E6; --header-bg: #343434; --header-text: #FFFFFF;
    --header-border: #D92B2B; --border: #9E9EA6; --text: #1E1E1E; --muted: #52525A;
    --accent: #D92B2B; --accent-hover: #B81D1D; --accent-text: #FFFFFF;
    --input-bg: #F4F4F6; --btn-bg: #C2C2C7; --btn-hover: #B2B2B9; --btn-border: #787880;
    --danger-bg: #B91C1C; --danger-hover: #991B1B; --danger-border: #7F1D1D; --danger-text: #FFFFFF;
    --pre-bg: #EBEBEF; --pre-text: #1E1E1E; --drop-bg: #DCDCE0;
    --warn-bg: #F5E6C8; --warn-border: #D92B2B; --warn-text: #343434;
    --badge-bg: #4A4A52; --badge-text: #FFFFFF; --badge-border: #787880;
    --badge-ok-bg: #166534; --badge-ok-text: #F0FDF4; --badge-ok-border: #4ADE80;
    --badge-err-bg: #991B1B; --badge-err-text: #FEF2F2; --badge-err-border: #F87171;
    --prog-grad: linear-gradient(90deg, #343434, #D92B2B);
    --svg-body: #343434; --svg-ic: #1E1E1E; --svg-pin-on: #D92B2B; --svg-pin-off: #787880;
    --svg-txt: #1E1E1E; --svg-sub: #D92B2B; --svg-ic-txt: #FFFFFF; --svg-ic-sub: #FCA5A5;
  }
  :root[data-theme="light"] {
    --bg: #F5F7FA; --panel: #FFFFFF; --header-bg: #FFFFFF; --header-text: #111827;
    --header-border: #D1D5DB; --border: #D1D5DB; --text: #111827; --muted: #6B7280;
    --accent: #2563EB; --accent-hover: #1D4ED8; --accent-text: #FFFFFF;
    --input-bg: #FFFFFF; --btn-bg: #F3F4F6; --btn-hover: #E5E7EB; --btn-border: #D1D5DB;
    --danger-bg: #DC2626; --danger-hover: #B91C1C; --danger-border: #991B1B; --danger-text: #FFFFFF;
    --pre-bg: #F9FAFB; --pre-text: #1F2937; --drop-bg: #F9FAFB;
    --warn-bg: #FEF3C7; --warn-border: #F59E0B; --warn-text: #78350F;
    --badge-bg: #E5E7EB; --badge-text: #1F2937; --badge-border: #D1D5DB;
    --badge-ok-bg: #DCFCE7; --badge-ok-text: #166534; --badge-ok-border: #86EFAC;
    --badge-err-bg: #FEE2E2; --badge-err-text: #991B1B; --badge-err-border: #FCA5A5;
    --prog-grad: linear-gradient(90deg, #2563EB, #16A34A);
    --svg-body: #E5E7EB; --svg-ic: #1F2937; --svg-pin-on: #2563EB; --svg-pin-off: #9CA3AF;
    --svg-txt: #374151; --svg-sub: #B45309; --svg-ic-txt: #F9FAFB; --svg-ic-sub: #93C5FD;
  }
  :root[data-theme="gray"] {
    --bg: #2E3238; --panel: #383D45; --header-bg: #25282D; --header-text: #F0F3F7;
    --header-border: #505762; --border: #505762; --text: #ECEFF4; --muted: #B4BCC8;
    --accent: #4C8BF5; --accent-hover: #3B78E7; --accent-text: #FFFFFF;
    --input-bg: #272B30; --btn-bg: #454C56; --btn-hover: #545C68; --btn-border: #5E6673;
    --danger-bg: #991B1B; --danger-hover: #7F1D1D; --danger-border: #7F1D1D; --danger-text: #FFFFFF;
    --pre-bg: #22252A; --pre-text: #E5E9F0; --drop-bg: #2C3036;
    --warn-bg: rgba(245,158,11,.15); --warn-border: rgba(245,158,11,.45); --warn-text: #FDE68A;
    --badge-bg: #454C56; --badge-text: #ECEFF4; --badge-border: #5E6673;
    --badge-ok-bg: rgba(34,197,94,.22); --badge-ok-text: #86EFAC; --badge-ok-border: rgba(34,197,94,.45);
    --badge-err-bg: rgba(239,68,68,.22); --badge-err-text: #FCA5A5; --badge-err-border: rgba(239,68,68,.45);
    --prog-grad: linear-gradient(90deg, #4C8BF5, #22C55E);
    --svg-body: #1E2126; --svg-ic: #111827; --svg-pin-on: #38BDF8; --svg-pin-off: #475569;
    --svg-txt: #CBD5E1; --svg-sub: #FACC15; --svg-ic-txt: #F8FAFC; --svg-ic-sub: #93C5FD;
  }
  :root[data-theme="dark"] {
    --bg: #0f131a; --panel: #171d27; --header-bg: #131922; --header-text: #e6edf7;
    --header-border: #2a3444; --border: #2a3444; --text: #e6edf7; --muted: #8b9bb4;
    --accent: #3b82f6; --accent-hover: #2563eb; --accent-text: #ffffff;
    --input-bg: #0d1117; --btn-bg: #212a38; --btn-hover: #2b374a; --btn-border: #2a3444;
    --danger-bg: #7f1d1d; --danger-hover: #991b1b; --danger-border: #991b1b; --danger-text: #ffffff;
    --pre-bg: #0b0e14; --pre-text: #e6edf7; --drop-bg: #10151e;
    --warn-bg: rgba(245,158,11,.12); --warn-border: rgba(245,158,11,.35); --warn-text: #fde68a;
    --badge-bg: #263142; --badge-text: #e6edf7; --badge-border: #2a3444;
    --badge-ok-bg: rgba(34,197,94,.18); --badge-ok-text: #86efac; --badge-ok-border: rgba(34,197,94,.4);
    --badge-err-bg: rgba(239,68,68,.18); --badge-err-text: #fca5a5; --badge-err-border: rgba(239,68,68,.4);
    --prog-grad: linear-gradient(90deg, #3b82f6, #22c55e);
    --svg-body: #0d1117; --svg-ic: #1e293b; --svg-pin-on: #38bdf8; --svg-pin-off: #334155;
    --svg-txt: #94a3b8; --svg-sub: #facc15; --svg-ic-txt: #f8fafc; --svg-ic-sub: #93c5fd;
  }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); }
  header { padding: 14px 22px; background: var(--header-bg); color: var(--header-text); border-bottom: 3px solid var(--header-border); display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; }
  header h1 { margin: 0; font-size: 18px; letter-spacing: 0.4px; }
  header .subver { font-weight: 400; font-size: 13px; opacity: 0.85; }
  .ctl-inline { display: flex; align-items: center; gap: 6px; font-size: 12px; }
  .ctl-inline select { width: auto; padding: 5px 8px; }
  .badge { padding: 4px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; background: var(--badge-bg); color: var(--badge-text); border: 1px solid var(--badge-border); }
  .badge.ok { background: var(--badge-ok-bg); color: var(--badge-ok-text); border: 1px solid var(--badge-ok-border); }
  .badge.err { background: var(--badge-err-bg); color: var(--badge-err-text); border: 1px solid var(--badge-err-border); }
  main { max-width: 1200px; margin: 18px auto; padding: 0 18px; display: grid; grid-template-columns: 360px 1fr; gap: 16px; }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 14px; margin-bottom: 16px; }
  .card h2 { margin: 0 0 10px; font-size: 14px; color: var(--accent); text-transform: uppercase; letter-spacing: .6px; }
  label { display: block; font-size: 12px; color: var(--muted); margin: 8px 0 4px; font-weight: 600; }
  input, select { width: 100%; padding: 8px 10px; border-radius: 6px; border: 1px solid var(--border); background: var(--input-bg); color: var(--text); font-size: 13px; }
  button { padding: 9px 13px; border-radius: 7px; border: 1px solid var(--btn-border); background: var(--btn-bg); color: var(--text); font-size: 13px; font-weight: 600; cursor: pointer; }
  button:hover:not(:disabled) { background: var(--btn-hover); }
  button:disabled { opacity: .45; cursor: not-allowed; }
  button.primary { background: var(--accent); border-color: var(--accent-hover); color: var(--accent-text); }
  button.primary:hover:not(:disabled) { background: var(--accent-hover); }
  button.danger { background: var(--danger-bg); border-color: var(--danger-border); color: var(--danger-text); }
  button.danger:hover:not(:disabled) { background: var(--danger-hover); }
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .kv { font-size: 12px; line-height: 1.6; color: var(--muted); }
  .kv strong { color: var(--text); }
  .warnbox { font-size: 12px; background: var(--warn-bg); border: 1px solid var(--warn-border); color: var(--warn-text); padding: 9px 10px; border-radius: 7px; margin-top: 10px; line-height: 1.5; }
  .dropzone { border: 2px dashed var(--border); border-radius: 8px; padding: 14px; text-align: center; cursor: pointer; background: var(--drop-bg); }
  .dropzone.drag { border-color: var(--accent); opacity: 0.9; }
  .progress-wrap { height: 18px; background: var(--input-bg); border-radius: 999px; overflow: hidden; border: 1px solid var(--border); margin: 10px 0 6px; }
  .progress-bar { height: 100%; width: 0%; background: var(--prog-grad); transition: width .12s linear; }
  pre { background: var(--pre-bg); color: var(--pre-text); border: 1px solid var(--border); border-radius: 7px; padding: 10px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; overflow: auto; max-height: 280px; margin: 8px 0 0; line-height: 1.45; }
  .chip-parts { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
  .chk { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text); margin: 6px 0 0; font-weight: 400; }
  .chk input { width: auto; }
  @media (max-width: 880px) { main { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<header>
  <h1>LEAPER-3C ROMライター UI <span class="subver" data-i18n="subver">(macOS版)</span></h1>
  <div class="row">
    <div class="ctl-inline">
      <span data-i18n="lblLang">言語:</span>
      <select id="langSelect" onchange="setLanguage(this.value)">
        <option value="ja">日本語</option>
        <option value="en">English</option>
      </select>
    </div>
    <div class="ctl-inline">
      <span data-i18n="lblTheme">テーマ:</span>
      <select id="themeSelect" onchange="setTheme(this.value)">
        <option value="famicom" data-i18n="themeFamicom">ファミコン (赤×クリーム白)</option>
        <option value="nes" data-i18n="themeNes">NES (グレー×赤)</option>
        <option value="light" data-i18n="themeLight">ライト</option>
        <option value="gray" data-i18n="themeGray">グレー</option>
        <option value="dark" data-i18n="themeDark">ダーク (オリジナル)</option>
      </select>
    </div>
    <span id="hwBadge" class="badge" data-i18n="badgeChecking">USB接続を確認中...</span>
    <button onclick="refreshStatus()" data-i18n="btnRescan">再検索</button>
  </div>
</header>
<main>
  <div>
    <div class="card">
      <h2 data-i18n="sec1">1. プログラマ＆ターゲットIC選択</h2>
      <div id="hwMeta" class="kv" data-i18n="hwChecking">接続確認中...</div>
      <label for="chipFilter" data-i18n="lblChipFilter">IC 検索（型番・メーカー）</label>
      <input id="chipFilter" placeholder="例: 27C256, 27C64, 39SF010A" oninput="filterChips()" />
      <label for="chipSelect" data-i18n="lblChipSelect">ターゲット IC</label>
      <select id="chipSelect" onchange="onSelectChip()"></select>
      <div id="chipMeta" class="kv" style="margin-top:8px;">-</div>
    </div>
    <div class="card">
      <h2 data-i18n="sec2">2. スレーブソケット挿し方ガイド</h2>
      <div style="display:flex;justify-content:center;padding:4px 0;">
        <svg id="socketSvg" width="250" height="190" viewBox="0 0 250 190" xmlns="http://www.w3.org/2000/svg"></svg>
      </div>
      <div class="warnbox" id="warnBox" data-i18n-html="warnHtml">
        <strong>注意:</strong><br/>
        • <strong>DC 12V ACアダプタ</strong> 接続＆電源ON必須<br/>
        • 挿すのは <strong>右側「SLAVE」ソケット</strong><br/>
        • <strong>切り欠き（1番ピン）は上（LCD側）</strong>、本体位置は <strong>下端（レバー側）揃え</strong>（28ピンICは上側2ピン空き）
      </div>
    </div>
  </div>
  <div>
    <div class="card">
      <h2 data-i18n="sec3">3. ROM / バイナリファイル読込（.bin / .nes）</h2>
      <div id="dropzone" class="dropzone" onclick="document.getElementById('fileInput').click()">
        <span data-i18n-html="dropHtml">ここに <code>.bin</code> または <code>.nes</code> をドロップ / クリックして選択</span>
        <input id="fileInput" type="file" style="display:none" onchange="handleFiles(this.files)" />
      </div>
      <div id="nesParts" class="chip-parts"></div>
      <div id="bufMeta" class="kv" style="margin-top:8px;" data-i18n="bufEmpty">ファイル未読込（読出しのみでも実行できます）</div>
      <div class="row" style="margin-top:8px;">
        <div style="flex:1;min-width:170px;">
          <label for="padMode" data-i18n="lblPadMode">サイズ不一致時の余白埋め</label>
          <select id="padMode">
            <option value="none" data-i18n="padNone">しない（IC容量と一致必須）</option>
            <option value="mirror" data-i18n="padMirror">ミラー繰り返し（8KB→32KB等）</option>
            <option value="ff" data-i18n="padFf">0xFF で余白を埋める</option>
          </select>
        </div>
        <div style="padding-top:18px;">
          <label class="chk"><input type="checkbox" id="chkErase" /> <span data-i18n="chkErase">書込み前に消去（Flash専用）</span></label>
          <label class="chk"><input type="checkbox" id="chkVerify" checked /> <span data-i18n="chkVerify">書込み後にベリファイ（照合）</span></label>
        </div>
      </div>
    </div>
    <div class="card">
      <h2 data-i18n="sec4">4. デバイス操作</h2>
      <div class="row">
        <button onclick="runAction('blank-check')" data-i18n="btnBlank">ブランクチェック</button>
        <button onclick="runAction('read')" data-i18n="btnRead">ICから読出し</button>
        <button onclick="runAction('verify')" data-i18n="btnVerify">ベリファイ（照合）</button>
        <button id="btnErase" class="danger" onclick="runAction('erase')" data-i18n="btnErase">消去（イレース）</button>
        <button class="primary" onclick="runAction('program')" data-i18n="btnProgram">書込み（プログラム）</button>
        <button id="btnDownload" disabled onclick="downloadLast()" data-i18n="btnDownload">読出し結果を保存 (.bin)</button>
      </div>
      <div class="progress-wrap"><div id="progBar" class="progress-bar"></div></div>
      <div id="progText" class="kv" data-i18n="progIdle">待機中</div>
      <pre id="logBox" data-i18n="logReady">準備完了。</pre>
    </div>
    <div class="card">
      <h2 data-i18n="sec5">5. HEX ダンプ表示</h2>
      <pre id="hexBox" data-i18n="hexEmpty">（データ未読込）</pre>
    </div>
  </div>
</main>
<script>
const I18N = {
  ja: {
    subver: "(macOS版)",
    lblLang: "言語:",
    lblTheme: "テーマ:",
    themeFamicom: "ファミコン (赤×クリーム白)",
    themeNes: "NES (グレー×赤)",
    themeLight: "ライト",
    themeGray: "グレー",
    themeDark: "ダーク (オリジナル)",
    badgeChecking: "USB接続を確認中...",
    btnRescan: "再検索",
    sec1: "1. プログラマ＆ターゲットIC選択",
    hwChecking: "接続確認中...",
    lblChipFilter: "IC 検索（型番・メーカー）",
    chipFilterPh: "例: 27C256, 27C64, 39SF010A",
    lblChipSelect: "ターゲット IC",
    sec2: "2. スレーブソケット挿し方ガイド",
    warnHtml: "<strong>注意:</strong><br/>• <strong>DC 12V ACアダプタ</strong> 接続＆電源ON必須<br/>• 挿すのは <strong>右側「SLAVE」ソケット</strong><br/>• <strong>切り欠き（1番ピン）は上（LCD側）</strong>、本体位置は <strong>下端（レバー側）揃え</strong>（28ピンICは上側2ピン空き）",
    sec3: "3. ROM / バイナリファイル読込（.bin / .nes）",
    dropHtml: "ここに <code>.bin</code> または <code>.nes</code> をドロップ / クリックして選択",
    bufEmpty: "ファイル未読込（読出しのみでも実行できます）",
    lblPadMode: "サイズ不一致時の余白埋め",
    padNone: "しない（IC容量と一致必須）",
    padMirror: "ミラー繰り返し（8KB→32KB等）",
    padFf: "0xFF で余白を埋める",
    chkErase: "書込み前に消去（Flash専用）",
    chkVerify: "書込み後にベリファイ（照合）",
    sec4: "4. デバイス操作",
    btnBlank: "ブランクチェック",
    btnRead: "ICから読出し",
    btnVerify: "ベリファイ（照合）",
    btnErase: "消去（イレース）",
    btnProgram: "書込み（プログラム）",
    btnDownload: "読出し結果を保存 (.bin)",
    progIdle: "待機中",
    logReady: "準備完了。",
    hexEmpty: "（データ未読込）",
    connectedBadge: fw => `接続済み (FW ${fw})`,
    disconnectedBadge: "未接続",
    connErrBadge: "接続エラー",
    notFound: "LEAPER-3C が見つかりません",
    hwMetaHtml: st => `<strong>${st.banner}</strong> | USB: ${st.location} | 対応IC: ${st.chip_count}種<br/>ステータス 0x42: <code>${st.status_hex}</code>`,
    chipMetaHtml: info => `容量: <strong>${info.size_bytes.toLocaleString()} B (${info.size_bytes/1024} KB)</strong> | 種類: <strong>${info.is_uv_eprom ? "UV-EPROM (27C系)" : "Flash / EEPROM"}</strong> | ~Vcc ${info.vcc} / ~Vpp ${info.vpp}`,
    svgTop: "↑ LCD側 (1番ピン/切り欠き上)",
    svg28: "上2ピン空き・下詰め",
    svg32: "ソケット全体を使用",
    svgBottom: "↓ レバー側 (底辺を揃える)",
    nesSplit: mapper => `iNES ROM (Mapper ${mapper}) を分割しました。書き込むバンクを選んでください:`,
    nesBtn: p => `${p.kind} をセット (${p.size/1024} KB / ${p.crc32})`,
    bufLoaded: p => `選択中: <strong>${p.name}</strong> (${p.size.toLocaleString()} bytes) | CRC32=<code>${p.crc32}</code> | SUM16=<code>${p.sum16}</code>`,
    alertNoChip: "IC を選択してください。",
    confirmAction: (act, c) => `${act.toUpperCase()} を実行しますか？\nIC: ${c.vendor} ${c.name}\n(右側SLAVEソケット・下詰め・12V電源を確認してください)`
  },
  en: {
    subver: "(macOS)",
    lblLang: "Language:",
    lblTheme: "Theme:",
    themeFamicom: "Famicom (Red & Cream)",
    themeNes: "NES (Gray & Red)",
    themeLight: "Light",
    themeGray: "Gray",
    themeDark: "Dark (Original)",
    badgeChecking: "Checking USB...",
    btnRescan: "Rescan",
    sec1: "1. Programmer & Target IC",
    hwChecking: "Checking connection...",
    lblChipFilter: "Search IC (Part / Vendor)",
    chipFilterPh: "e.g. 27C256, 27C64, 39SF010A",
    lblChipSelect: "Target IC",
    sec2: "2. Slave Socket Placement Guide",
    warnHtml: "<strong>Notice:</strong><br/>• <strong>DC 12V AC Adapter</strong> & Power ON required<br/>• Use the <strong>right-side SLAVE socket</strong><br/>• <strong>Notch (Pin 1) faces UP (LCD)</strong>, aligned to <strong>bottom (lever)</strong> (28-pin leaves top 2 pins empty)",
    sec3: "3. Load ROM / Binary File (.bin / .nes)",
    dropHtml: "Drop <code>.bin</code> or <code>.nes</code> here, or click to select",
    bufEmpty: "No file loaded (Read can run without a file)",
    lblPadMode: "Padding on size mismatch",
    padNone: "None (exact IC size required)",
    padMirror: "Mirror repeat (e.g. 8KB → 32KB)",
    padFf: "Pad remaining space with 0xFF",
    chkErase: "Erase before write (Flash only)",
    chkVerify: "Verify after write",
    sec4: "4. Device Operations",
    btnBlank: "Blank Check",
    btnRead: "Read IC",
    btnVerify: "Verify",
    btnErase: "Erase",
    btnProgram: "Program (Write)",
    btnDownload: "Save Readback (.bin)",
    progIdle: "Idle",
    logReady: "Ready.",
    hexEmpty: "(No data loaded)",
    connectedBadge: fw => `Connected (FW ${fw})`,
    disconnectedBadge: "Disconnected",
    connErrBadge: "Connection Error",
    notFound: "LEAPER-3C not found",
    hwMetaHtml: st => `<strong>${st.banner}</strong> | USB: ${st.location} | Chips: ${st.chip_count}<br/>Status 0x42: <code>${st.status_hex}</code>`,
    chipMetaHtml: info => `Size: <strong>${info.size_bytes.toLocaleString()} B (${info.size_bytes/1024} KB)</strong> | Type: <strong>${info.is_uv_eprom ? "UV-EPROM (27C)" : "Flash / EEPROM"}</strong> | ~Vcc ${info.vcc} / ~Vpp ${info.vpp}`,
    svgTop: "↑ LCD Side (Pin 1 / Notch UP)",
    svg28: "Top 2 pins empty / Bottom-aligned",
    svg32: "Uses full 32-pin socket",
    svgBottom: "↓ Lever Side (Align bottom edge)",
    nesSplit: mapper => `iNES ROM (Mapper ${mapper}) split. Choose bank to load:`,
    nesBtn: p => `Load ${p.kind} (${p.size/1024} KB / ${p.crc32})`,
    bufLoaded: p => `Loaded: <strong>${p.name}</strong> (${p.size.toLocaleString()} bytes) | CRC32=<code>${p.crc32}</code> | SUM16=<code>${p.sum16}</code>`,
    alertNoChip: "Please select an IC.",
    confirmAction: (act, c) => `Run ${act.toUpperCase()}?\nIC: ${c.vendor} ${c.name}\n(Confirm right-side SLAVE socket, bottom-aligned, 12V power ON)`
  }
};
let curLang = localStorage.getItem("leaper3c_lang") || "ja";
if (!I18N[curLang]) curLang = "ja";
let curTheme = localStorage.getItem("leaper3c_theme") || "famicom";
if (!["famicom", "nes", "light", "gray", "dark"].includes(curTheme)) curTheme = "famicom";
let lastStatusData = null;

function t(key) {
  return (I18N[curLang] && I18N[curLang][key]) || I18N.ja[key] || key;
}

function setTheme(theme) {
  curTheme = ["famicom", "nes", "light", "gray", "dark"].includes(theme) ? theme : "famicom";
  localStorage.setItem("leaper3c_theme", curTheme);
  document.documentElement.setAttribute("data-theme", curTheme);
  const sel = document.getElementById("themeSelect");
  if (sel) sel.value = curTheme;
  if (currentChip) drawSocketSvg(currentChip.pin_count);
}

function setLanguage(lang) {
  curLang = I18N[lang] ? lang : "ja";
  localStorage.setItem("leaper3c_lang", curLang);
  document.documentElement.lang = curLang;
  const sel = document.getElementById("langSelect");
  if (sel) sel.value = curLang;
  document.querySelectorAll("[data-i18n]").forEach(el => {
    el.textContent = t(el.getAttribute("data-i18n"));
  });
  document.querySelectorAll("[data-i18n-html]").forEach(el => {
    el.innerHTML = t(el.getAttribute("data-i18n-html"));
  });
  const cf = document.getElementById("chipFilter");
  if (cf) cf.placeholder = t("chipFilterPh");
  if (lastStatusData) renderStatusData(lastStatusData);
  if (currentChip) {
    document.getElementById("chipMeta").innerHTML = t("chipMetaHtml")(currentChip);
    drawSocketSvg(currentChip.pin_count);
  }
}

let allChips = [];
let currentChip = null;
let loadedBuffer = { name: "", b64: "", size: 0 };
let pollTimer = null;

async function api(url, body) {
  const res = await fetch(url, body ? {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  } : undefined);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function renderStatusData(st) {
  const badge = document.getElementById("hwBadge");
  const meta = document.getElementById("hwMeta");
  if (st.connected) {
    badge.className = "badge ok";
    badge.textContent = t("connectedBadge")(st.firmware);
    meta.innerHTML = t("hwMetaHtml")(st);
  } else {
    badge.className = "badge err";
    badge.textContent = t("disconnectedBadge");
    meta.textContent = st.error || t("notFound");
  }
}

async function refreshStatus() {
  const badge = document.getElementById("hwBadge");
  const meta = document.getElementById("hwMeta");
  try {
    const st = await api("/api/status");
    lastStatusData = st;
    renderStatusData(st);
    if (st.connected && allChips.length === 0) await loadChips();
  } catch (e) {
    badge.className = "badge err";
    badge.textContent = t("connErrBadge");
    meta.textContent = e.message;
  }
}

async function loadChips() {
  const data = await api("/api/chips");
  allChips = data.chips || [];
  renderChipSelect(allChips);
}

function filterChips() {
  const q = document.getElementById("chipFilter").value.trim().toUpperCase();
  const list = q ? allChips.filter(c => c.label.toUpperCase().includes(q)) : allChips;
  renderChipSelect(list);
}

function renderChipSelect(list) {
  const sel = document.getElementById("chipSelect");
  sel.innerHTML = "";
  for (const c of list) {
    const opt = document.createElement("option");
    opt.value = JSON.stringify({ name: c.name, vendor: c.vendor });
    opt.textContent = c.label;
    if (c.name === "27C256" && c.vendor === "27XXX") opt.selected = true;
    sel.appendChild(opt);
  }
  if (sel.options.length > 0 && sel.selectedIndex < 0) sel.selectedIndex = 0;
  onSelectChip();
}

async function onSelectChip() {
  const sel = document.getElementById("chipSelect");
  if (!sel.value) return;
  const spec = JSON.parse(sel.value);
  try {
    const info = await api("/api/chip-info", spec);
    currentChip = info;
    document.getElementById("chipMeta").innerHTML = t("chipMetaHtml")(info);
    document.getElementById("btnErase").disabled = info.is_uv_eprom;
    drawSocketSvg(info.pin_count);
  } catch (e) {
    document.getElementById("chipMeta").textContent = e.message;
  }
}

function drawSocketSvg(pins) {
  const svg = document.getElementById("socketSvg");
  const is28 = pins === 28;
  let holes = "";
  for (let i = 0; i < 16; i++) {
    const y = 26 + i * 9;
    const occupied = !is28 || i >= 2;
    const c = occupied ? "var(--svg-pin-on)" : "var(--svg-pin-off)";
    holes += `<rect x="74" y="${y}" width="8" height="5" rx="1" fill="${c}"/>`;
    holes += `<rect x="168" y="${y}" width="8" height="5" rx="1" fill="${c}"/>`;
  }
  const icTop = is28 ? 42 : 24;
  const icH = is28 ? 128 : 146;
  svg.innerHTML = `
    <text x="125" y="14" fill="var(--svg-txt)" font-size="11" text-anchor="middle">${t("svgTop")}</text>
    <rect x="65" y="20" width="120" height="154" rx="6" fill="var(--svg-body)" stroke="var(--border)" stroke-width="2"/>
    ${holes}
    <rect x="88" y="${icTop}" width="74" height="${icH}" rx="4" fill="var(--svg-ic)" stroke="var(--accent)" stroke-width="2"/>
    <path d="M117,${icTop} A8,8 0 0,0 133,${icTop}" fill="var(--svg-body)" stroke="var(--accent)" stroke-width="1.5"/>
    <circle cx="96" cy="${icTop + 9}" r="3" fill="var(--svg-pin-on)"/>
    <text x="125" y="${icTop + 44}" fill="var(--svg-ic-txt)" font-size="12" font-weight="bold" text-anchor="middle">${pins}-pin IC</text>
    <text x="125" y="${icTop + 62}" fill="var(--svg-ic-sub)" font-size="10" text-anchor="middle">${is28 ? t("svg28") : t("svg32")}</text>
    <text x="125" y="186" fill="var(--svg-sub)" font-size="11" font-weight="bold" text-anchor="middle">${t("svgBottom")}</text>
  `;
}

const dz = document.getElementById("dropzone");
dz.addEventListener("dragover", e => { e.preventDefault(); dz.classList.add("drag"); });
dz.addEventListener("dragleave", () => dz.classList.remove("drag"));
dz.addEventListener("drop", e => { e.preventDefault(); dz.classList.remove("drag"); handleFiles(e.dataTransfer.files); });

function handleFiles(files) {
  if (!files || !files[0]) return;
  const f = files[0];
  const reader = new FileReader();
  reader.onload = async () => {
    const bytes = new Uint8Array(reader.result);
    let bin = "";
    for (let i = 0; i < bytes.length; i += 0x8000) {
      bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    }
    const b64 = btoa(bin);
    try {
      const info = await api("/api/inspect-file", { filename: f.name, data_b64: b64 });
      const partsDiv = document.getElementById("nesParts");
      partsDiv.innerHTML = "";
      if (info.is_nes) {
        document.getElementById("bufMeta").innerHTML = t("nesSplit")(info.mapper);
        info.parts.forEach((p, idx) => {
          const btn = document.createElement("button");
          btn.textContent = t("nesBtn")(p);
          btn.onclick = () => applyBuffer(p);
          partsDiv.appendChild(btn);
          if (idx === 0) applyBuffer(p);
        });
      } else {
        applyBuffer(info);
      }
    } catch (e) {
      alert(e.message);
    }
  };
  reader.readAsArrayBuffer(f);
}

function applyBuffer(part) {
  loadedBuffer = { name: part.name, b64: part.b64, size: part.size };
  document.getElementById("bufMeta").innerHTML = t("bufLoaded")(part);
  document.getElementById("hexBox").textContent = part.hex_preview || "";
  if (currentChip && part.size < currentChip.size_bytes && currentChip.size_bytes % part.size === 0) {
    document.getElementById("padMode").value = "mirror";
  } else {
    document.getElementById("padMode").value = "none";
  }
}

async function runAction(action) {
  if (!currentChip) { alert(t("alertNoChip")); return; }
  if ((action === "program" || action === "erase") && !confirm(t("confirmAction")(action, currentChip))) {
    return;
  }
  try {
    await api("/api/action", {
      action,
      chip: currentChip.name,
      vendor: currentChip.vendor,
      pad: document.getElementById("padMode").value,
      erase: document.getElementById("chkErase").checked,
      verify: document.getElementById("chkVerify").checked,
      data_b64: loadedBuffer.b64,
    });
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(pollJob, 150);
  } catch (e) {
    alert(e.message);
  }
}

async function pollJob() {
  const j = await api("/api/job");
  document.getElementById("progBar").style.width = `${j.pct}%`;
  document.getElementById("progText").textContent = `${j.phase} (${j.done}/${j.total} - ${j.pct}%)` +
    (j.result ? ` — ${j.result.message}` : "") + (j.error ? ` — ERROR: ${j.error}` : "");
  document.getElementById("logBox").textContent = (j.logs || []).join("\n");
  const lb = document.getElementById("logBox");
  lb.scrollTop = lb.scrollHeight;
  if (!j.running) {
    clearInterval(pollTimer);
    pollTimer = null;
    if (j.result && j.result.hex_preview) {
      document.getElementById("hexBox").textContent = j.result.hex_preview;
    }
    if (j.result && j.result.download_ready) {
      document.getElementById("btnDownload").disabled = false;
    }
  }
}

function downloadLast() {
  window.location.href = "/api/download-last";
}

setTheme(curTheme);
setLanguage(curLang);
refreshStatus();
</script>
</body>
</html>
"""


def make_handler(state: UiState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _send_json(self, obj: Any, status: int = HTTPStatus.OK) -> None:
            raw = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            return json.loads(raw.decode("utf-8"))

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/":
                raw = HTML_PAGE.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if self.path == "/api/status":
                self._send_json(state.get_status())
                return
            if self.path == "/api/chips":
                try:
                    self._send_json({"chips": state.get_chips()})
                except Exception as exc:
                    self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            if self.path == "/api/job":
                with state.job_lock:
                    self._send_json(dict(state.job))
                return
            if self.path == "/api/download-last":
                data = state.last_buffer
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", f'attachment; filename="{state.last_filename}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            try:
                body = self._read_json()
                if self.path == "/api/chip-info":
                    self._send_json(state.get_chip_info(body.get("name", ""), body.get("vendor", "")))
                    return
                if self.path == "/api/inspect-file":
                    raw = base64.b64decode(body.get("data_b64", ""))
                    self._send_json(state.inspect_uploaded_file(body.get("filename", "rom.bin"), raw))
                    return
                if self.path == "/api/action":
                    jid = state.start_action(body)
                    self._send_json({"job_id": jid})
                    return
                self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            except Exception as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    return Handler


def serve_ui(port: int = 8765, open_browser: bool = True) -> int:
    state = UiState()
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    actual_port = server.server_address[1]
    url = f"http://127.0.0.1:{actual_port}/"
    print(f"LEAPER-3C Web UI running at {url} (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0

