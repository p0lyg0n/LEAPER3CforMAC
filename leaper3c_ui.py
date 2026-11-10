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


OFFLINE_CHIPS: list[dict[str, Any]] = [
    {"name": "27C64", "vendor": "27XXX", "size_bytes": 8192, "is_uv_eprom": True, "pin_count": 28},
    {"name": "27C128", "vendor": "27XXX", "size_bytes": 16384, "is_uv_eprom": True, "pin_count": 28},
    {"name": "27C256", "vendor": "27XXX", "size_bytes": 32768, "is_uv_eprom": True, "pin_count": 28},
    {"name": "27C512", "vendor": "27XXX", "size_bytes": 65536, "is_uv_eprom": True, "pin_count": 28},
    {"name": "27C010", "vendor": "27XXX", "size_bytes": 131072, "is_uv_eprom": True, "pin_count": 32},
    {"name": "27C020", "vendor": "27XXX", "size_bytes": 262144, "is_uv_eprom": True, "pin_count": 32},
    {"name": "27C040", "vendor": "27XXX", "size_bytes": 524288, "is_uv_eprom": True, "pin_count": 32},
    {"name": "27C080", "vendor": "27XXX", "size_bytes": 1048576, "is_uv_eprom": True, "pin_count": 32},
    {"name": "28C256", "vendor": "ATMEL", "size_bytes": 32768, "is_uv_eprom": False, "pin_count": 28},
    {"name": "39SF010A", "vendor": "SST", "size_bytes": 131072, "is_uv_eprom": False, "pin_count": 32},
    {"name": "39SF020A", "vendor": "SST", "size_bytes": 262144, "is_uv_eprom": False, "pin_count": 32},
    {"name": "39SF040", "vendor": "SST", "size_bytes": 524288, "is_uv_eprom": False, "pin_count": 32},
    {"name": "29F010", "vendor": "AMD", "size_bytes": 131072, "is_uv_eprom": False, "pin_count": 32},
    {"name": "29F040", "vendor": "AMD", "size_bytes": 524288, "is_uv_eprom": False, "pin_count": 32},
]


def _offline_chip_list() -> list[dict[str, Any]]:
    return [
        {
            "vendor_id": 0,
            "vendor": c["vendor"],
            "dev_id": 0,
            "name": c["name"],
            "label": f"{c['name']} @ {c['vendor']}",
            "is_uv_eprom": c["is_uv_eprom"],
        }
        for c in OFFLINE_CHIPS
    ]


def _norm_lang(lang: str | None) -> str:
    return "en" if str(lang or "").strip().lower().startswith("en") else "ja"


def _localize_error(msg: str, lang: str = "ja") -> str:
    if _norm_lang(lang) != "en":
        return msg
    text = msg
    replacements = [
        ("見つかりません。USBケーブルと12V電源スイッチを確認してください。", "not found. Please check the USB cable and 12V power switch."),
        ("libusb が見つかりません。`brew install libusb` を実行してください。", "libusb not found. Please run `brew install libusb`."),
        ("USBインターフェース(0)を確保できませんでした:", "Could not claim USB interface (0):"),
        ("IC向き・接触・12V電源を確認してください", "Check IC orientation, socket contact, and 12V power"),
        ("過電流を検出しました！ICを逆挿ししていないか直ちに確認してください", "Over-current detected! Immediately verify the IC is not inserted backwards."),
        ("エラーコード ", "Error code "),
        ("失敗:", "failed:"),
        ("失敗 ", "failed "),
        ("対応ICリストに ", "Not found in device IC list: "),
        (" が見つかりません。`list` コマンドで確認してください。", " (check with `list`)."),
        (" が見つかりません。", " not found."),
        ("書き込むファイルデータがありません。", "No file data loaded for writing."),
        ("照合するファイルデータがありません。", "No file data loaded for verification."),
        ("有効な iNES (.nes) ヘッダではありません:", "Invalid iNES (.nes) header:"),
        ("は UV-EPROM (紫外線消去型) のため、電気消去コマンドは実行できません。紫外線イレーサーで消去してください。", "is a UV-EPROM and cannot be electrically erased. Use a UV eraser."),
    ]
    for jp, en in replacements:
        text = text.replace(jp, en)
    return text


def _find_offline_chip(chip_name: str, vendor_name: str, lang: str = "ja") -> dict[str, Any]:
    q_name = chip_name.strip().upper()
    q_vendor = vendor_name.strip().upper()
    for c in OFFLINE_CHIPS:
        if c["name"].upper() == q_name and (not q_vendor or c["vendor"].upper() == q_vendor):
            sz = int(c["size_bytes"])
            return {
                "source": "offline",
                "vendor": c["vendor"],
                "vendor_id": 0,
                "name": c["name"],
                "dev_id": 0,
                "size_bytes": sz,
                "blocks_512": sz // 512,
                "is_uv_eprom": bool(c["is_uv_eprom"]),
                "pin_count": int(c["pin_count"]),
                "vcc": "-",
                "vpp": "-",
            }
    if _norm_lang(lang) == "en":
        raise leaper3c.LeaperError(f"Chip '{chip_name}' not found in offline reference catalog.")
    raise leaper3c.LeaperError(f"未接続時の参照カタログに '{chip_name}' が見つかりません。")


class UiState:
    """Thread-safe controller bridging the HTTP UI and LEAPER-3C hardware."""

    def __init__(self, backend_factory: Callable[[], Any] = leaper3c.PyUsbBackend) -> None:
        self.backend_factory = backend_factory
        self.usb_lock = threading.Lock()
        self.job_lock = threading.Lock()
        self.last_buffer: bytes = b""
        self.last_filename: str = "readback.bin"
        self.cached_chip_count: int = 0
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
            self.job["logs"] = self.job["logs"][-2000:]

    def clear_logs(self) -> None:
        with self.job_lock:
            self.job["logs"] = []

    def get_status(self, quick: bool = False, lang: str = "ja") -> dict[str, Any]:
        lang = _norm_lang(lang)
        acquired = self.usb_lock.acquire(blocking=not quick)
        if not acquired:
            return {"busy": True}
        backend = None
        try:
            try:
                backend = self.backend_factory()
            except leaper3c.LeaperError as exc:
                return {"connected": False, "error": _localize_error(str(exc), lang)}
            try:
                lp = leaper3c.Leaper3C(backend)
                info = lp.get_info()
                st = lp.read_status(timeout=1500 if quick else 5000)
                if not quick or self.cached_chip_count == 0:
                    self.cached_chip_count = len(lp.list_chips())
                loc = getattr(backend, "usb_location", "mock")
                return {
                    "connected": True,
                    "location": loc,
                    "banner": info.banner,
                    "firmware": info.fw_version,
                    "table_base": f"0x{info.table_base_addr:04X}",
                    "chip_count": self.cached_chip_count,
                    "status_hex": st.hex(" "),
                }
            except Exception as exc:
                return {"connected": False, "error": _localize_error(str(exc), lang)}
            finally:
                close_fn = getattr(backend, "close", None)
                if close_fn:
                    close_fn()
        finally:
            self.usb_lock.release()

    def get_chips(self) -> tuple[list[dict[str, Any]], str]:
        with self.usb_lock:
            try:
                backend = self.backend_factory()
            except leaper3c.LeaperError:
                return _offline_chip_list(), "offline"
            try:
                lp = leaper3c.Leaper3C(backend)
                chips = lp.list_chips()
                return (
                    [
                        {
                            "vendor_id": c.vendor_id,
                            "vendor": c.vendor_name,
                            "dev_id": c.dev_id,
                            "name": c.name,
                            "label": f"{c.name} @ {c.vendor_name}",
                            "is_uv_eprom": c.is_uv_eprom,
                        }
                        for c in chips
                    ],
                    "hardware",
                )
            except leaper3c.LeaperError:
                return _offline_chip_list(), "offline"
            finally:
                close_fn = getattr(backend, "close", None)
                if close_fn:
                    close_fn()

    def get_chip_info(self, chip_name: str, vendor_name: str, lang: str = "ja") -> dict[str, Any]:
        lang = _norm_lang(lang)
        with self.usb_lock:
            try:
                backend = self.backend_factory()
            except leaper3c.LeaperError:
                return _find_offline_chip(chip_name, vendor_name, lang=lang)
            try:
                lp = leaper3c.Leaper3C(backend)
                chip = lp.find_chip(chip_name, vendor_name or None)
                params = lp.select_chip(chip)
                pin_count = 28 if any(k in chip.name for k in ("27C64", "27C128", "27C256", "27C512", "28C256")) else 32
                return {
                    "source": "hardware",
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
            except leaper3c.LeaperError:
                try:
                    return _find_offline_chip(chip_name, vendor_name, lang=lang)
                except leaper3c.LeaperError:
                    raise
            finally:
                close_fn = getattr(backend, "close", None)
                if close_fn:
                    close_fn()

    def inspect_uploaded_file(self, filename: str, data: bytes, lang: str = "ja") -> dict[str, Any]:
        lang = _norm_lang(lang)
        if len(data) >= 16 and data[:4] == b"NES\x1a":
            prg_banks = data[4]
            chr_banks = data[5]
            flags6 = data[6]
            flags7 = data[7]
            is_nes2 = (flags7 & 0x0C) == 0x08
            if is_nes2:
                prg_banks |= (data[9] & 0x0F) << 8
                chr_banks |= ((data[9] >> 4) & 0x0F) << 8
            mapper = ((flags6 >> 4) & 0x0F) | (flags7 & 0xF0)
            trainer_bytes = 512 if (flags6 & 0x04) else 0
            offset = 16 + trainer_bytes
            nes_format = "NES 2.0" if is_nes2 else "iNES"
            prg_size = prg_banks * 16384
            chr_size = chr_banks * 8192
            if len(data) < offset + prg_size + chr_size:
                msg = (
                    ".nes file is smaller than specified in its iNES header."
                    if lang == "en"
                    else ".nes ファイルサイズがヘッダ定義より不足しています。"
                )
                raise leaper3c.LeaperError(msg)
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
            if lang == "en":
                trainer_note = " incl. 512B trainer" if trainer_bytes else ""
                self._log(
                    f"{nes_format} header detected ({offset} bytes{trainer_note}) -> "
                    f"stripped automatically; PRG={len(prg)}B, CHR={len(chr_data)}B ({filename})"
                )
            else:
                trainer_note = " / Trainer 512B 含む" if trainer_bytes else ""
                self._log(
                    f"{nes_format} ヘッダ検出 ({offset} bytes{trainer_note}) → "
                    f"ヘッダを自動削除して PRG={len(prg)}B / CHR={len(chr_data)}B を抽出 ({filename})"
                )
            return {
                "is_nes": True,
                "filename": filename,
                "nes_format": nes_format,
                "header_bytes": offset,
                "trainer_bytes": trainer_bytes,
                "mapper": mapper,
                "prg_size": len(prg),
                "chr_size": len(chr_data),
                "parts": parts,
            }

        if lang == "en":
            self._log(f"Loaded binary: {filename} ({len(data)} bytes / no iNES header)")
        else:
            self._log(f"バイナリ読込: {filename} ({len(data)} bytes / iNES ヘッダなし)")
        return {
            "is_nes": False,
            "filename": filename,
            "name": filename,
            "size": len(data),
            "crc32": f"0x{zlib.crc32(data) & 0xFFFFFFFF:08X}",
            "sum16": f"0x{leaper3c.sum16(data):08X}",
            "sha256": hashlib.sha256(data).hexdigest(),
            "b64": base64.b64encode(data).decode("ascii"),
            "hex_preview": format_hex_preview(data, 512),
        }

    def start_action(self, payload: dict[str, Any], lang: str = "ja") -> int:
        lang = _norm_lang(lang)
        payload_with_lang = dict(payload)
        payload_with_lang["lang"] = lang
        with self.job_lock:
            if self.job["running"]:
                msg = (
                    "Another operation is already running. Please wait."
                    if lang == "en"
                    else "別の処理が実行中です。完了までお待ちください。"
                )
                raise leaper3c.LeaperError(msg)
            job_id = int(self.job["id"]) + 1
            self.job.update(
                {
                    "id": job_id,
                    "running": True,
                    "action": str(payload.get("action", "")),
                    "phase": "Starting" if lang == "en" else "開始中",
                    "done": 0,
                    "total": 0,
                    "pct": 0,
                    "ok": None,
                    "error": None,
                    "result": None,
                }
            )

        t = threading.Thread(target=self._run_action_worker, args=(job_id, payload_with_lang), daemon=True)
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
        lang = _norm_lang(str(payload.get("lang", "ja")))
        en = lang == "en"
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
                        self._update_progress("Blank checking..." if en else "ブランクチェック中...", 0, 1)
                        st = lp.blank_check(chip)
                        self._update_progress("Blank check done" if en else "ブランクチェック完了", 1, 1)
                        self._log(("Blank check OK" if en else "ブランクチェック 正常") + f" (status={st.hex(' ')})")
                        res = {
                            "message": "Blank Check PASS (all 0xFF)" if en else "ブランクチェック PASS（全 0xFF）",
                            "status": st.hex(" "),
                        }

                    elif action == "erase":
                        self._update_progress("Erasing device..." if en else "デバイス消去中...", 0, 1)
                        st = lp.erase(chip)
                        self._update_progress("Erase complete" if en else "消去完了", 1, 1)
                        self._log(("Erase OK" if en else "デバイス消去 正常") + f" (status={st.hex(' ')})")
                        res = {"message": "Erase PASS" if en else "消去 (イレース) PASS", "status": st.hex(" ")}

                    elif action == "read":
                        data = lp.read_chip(
                            chip,
                            progress_cb=lambda d, tot: self._update_progress("Reading..." if en else "読出し中", d, tot),
                        )
                        self.last_buffer = data
                        self.last_filename = f"{chip.name.lower()}_readback.bin"
                        crc = f"0x{zlib.crc32(data) & 0xFFFFFFFF:08X}"
                        s16 = f"0x{leaper3c.sum16(data):08X}"
                        sha = hashlib.sha256(data).hexdigest()
                        self._log(("Read complete:" if en else "読出し完了:") + f" {len(data)} bytes CRC32={crc}")
                        res = {
                            "message": f"Read PASS ({len(data)} bytes)" if en else f"読出し PASS ({len(data)} バイト)",
                            "size": len(data),
                            "crc32": crc,
                            "sum16": s16,
                            "sha256": sha,
                            "hex_preview": format_hex_preview(data, 1024),
                            "download_ready": True,
                        }

                    elif action == "verify":
                        if not raw:
                            raise leaper3c.LeaperError("No file data loaded for verification." if en else "照合するファイルデータがありません。")
                        expected = leaper3c.pad_image_to_chip(raw, params.size_bytes, pad_mode)
                        actual = lp.read_chip(
                            chip,
                            progress_cb=lambda d, tot: self._update_progress("Reading to verify..." if en else "ベリファイ読出し中", d, tot),
                        )
                        mm = leaper3c.verify_buffers(expected, actual)
                        if mm is not None:
                            off, exp_b, act_b = mm
                            raise leaper3c.LeaperError(
                                f"Mismatch at 0x{off:05X}: expected=0x{exp_b:02X}, actual=0x{act_b:02X}"
                                if en
                                else f"不一致 0x{off:05X}: 期待値=0x{exp_b:02X}, 実読値=0x{act_b:02X}"
                            )
                        crc = f"0x{zlib.crc32(actual) & 0xFFFFFFFF:08X}"
                        self._log(("Verify OK: 100% match" if en else "ベリファイ正常: 100% 一致") + f" CRC32={crc}")
                        res = {
                            "message": f"Verify PASS (100% match / CRC32={crc})" if en else f"ベリファイ PASS (100% 一致 / CRC32={crc})",
                            "crc32": crc,
                        }

                    elif action == "program":
                        if not raw:
                            raise leaper3c.LeaperError("No file data loaded for writing." if en else "書き込むファイルデータがありません。")
                        image = leaper3c.pad_image_to_chip(raw, params.size_bytes, pad_mode)
                        if do_erase and not chip.is_uv_eprom:
                            self._update_progress("Pre-erasing..." if en else "事前消去中...", 0, 1)
                            lp.erase(chip)
                            self._log("Pre-erase OK" if en else "事前消去 正常")

                        self._update_progress("Pre-blank checking..." if en else "事前ブランクチェック中...", 0, 1)
                        lp.blank_check(chip)
                        self._log("Pre-blank check OK" if en else "事前ブランクチェック 正常")

                        t0 = time.time()
                        lp.program_chip(
                            chip,
                            image,
                            progress_cb=lambda d, tot: self._update_progress("Programming..." if en else "書込み中", d, tot),
                        )
                        elapsed = time.time() - t0
                        self._log(f"Program complete ({elapsed:.2f}s)" if en else f"書込み完了 ({elapsed:.2f}秒)")

                        if do_verify:
                            actual = lp.read_chip(
                                chip,
                                progress_cb=lambda d, tot: self._update_progress("Verifying..." if en else "ベリファイ照合中", d, tot),
                            )
                            mm = leaper3c.verify_buffers(image, actual)
                            if mm is not None:
                                off, exp_b, act_b = mm
                                raise leaper3c.LeaperError(
                                    f"Verify mismatch at 0x{off:05X}: expected=0x{exp_b:02X}, actual=0x{act_b:02X}"
                                    if en
                                    else f"ベリファイ不一致 0x{off:05X}: 期待値=0x{exp_b:02X}, 実読値=0x{act_b:02X}"
                                )
                            self._log("Verify OK (100% match)" if en else "ベリファイ 正常 (100% 一致)")

                        crc = f"0x{zlib.crc32(image) & 0xFFFFFFFF:08X}"
                        msg_done = (
                            f"Program{' & Verify' if do_verify else ''} PASS ({elapsed:.2f}s, CRC32={crc})"
                            if en
                            else f"書込み{'＆ベリファイ' if do_verify else ''} PASS ({elapsed:.2f}秒, CRC32={crc})"
                        )
                        res = {"message": msg_done, "crc32": crc, "elapsed": round(elapsed, 2)}
                    else:
                        raise leaper3c.LeaperError(f"Unsupported action: {action}" if en else f"未対応のアクション: {action}")

                    with self.job_lock:
                        if self.job["id"] == job_id:
                            self.job["running"] = False
                            self.job["ok"] = True
                            self.job["pct"] = 100
                            self.job["phase"] = "Complete" if en else "完了"
                            self.job["result"] = res
                finally:
                    close_fn = getattr(backend, "close", None)
                    if close_fn:
                        close_fn()
        except Exception as exc:
            err_text = _localize_error(str(exc), lang)
            self._log(f"ERROR: {err_text}")
            with self.job_lock:
                if self.job["id"] == job_id:
                    self.job["running"] = False
                    self.job["ok"] = False
                    self.job["phase"] = "Failed" if en else "失敗"
                    self.job["error"] = err_text


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
  .card h2 { margin: 0 0 12px; font-size: 14px; color: var(--text); display: flex; align-items: center; gap: 8px; letter-spacing: .3px; }
  .step-pill { display: inline-flex; align-items: center; justify-content: center; background: var(--accent); color: var(--accent-text); border: 1.5px solid var(--header-border); font-size: 11px; font-weight: 800; padding: 3px 10px; border-radius: 999px; letter-spacing: .6px; box-shadow: 0 2px 0 rgba(0,0,0,0.18); flex-shrink: 0; }
  .hw-bar { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 8px; flex-wrap: wrap; }
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
  .errbox { font-size: 12px; background: var(--badge-err-bg); border: 1.5px solid var(--badge-err-border); color: var(--badge-err-text); padding: 9px 11px; border-radius: 7px; margin-bottom: 8px; line-height: 1.55; }
  .infobox { font-size: 12px; background: var(--badge-ok-bg); border: 1px solid var(--badge-ok-border); color: var(--badge-ok-text); padding: 9px 11px; border-radius: 7px; margin-top: 8px; line-height: 1.5; }
  .infobox.plain { background: var(--drop-bg); border-color: var(--border); color: var(--text); }
  .btn-xs { padding: 4px 9px; font-size: 11px; border-radius: 5px; }
  .dropzone { border: 2.5px dashed var(--border); border-radius: 10px; min-height: 120px; padding: 26px 18px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 6px; text-align: center; cursor: pointer; background: var(--drop-bg); font-size: 14px; font-weight: 600; transition: border-color .15s, background .15s, box-shadow .15s; }
  .dropzone:hover { border-color: var(--accent); }
  .dropzone .dropsub { font-size: 12px; font-weight: 400; color: var(--muted); }
  .dropzone.drag { border-color: var(--accent); background: var(--panel); box-shadow: inset 0 0 0 2px var(--accent); }
  .progress-wrap { height: 18px; background: var(--input-bg); border-radius: 999px; overflow: hidden; border: 1px solid var(--border); margin: 10px 0 6px; }
  .progress-bar { height: 100%; width: 0%; background: var(--prog-grad); transition: width .12s linear; }
  pre { background: var(--pre-bg); color: var(--pre-text); border: 1px solid var(--border); border-radius: 7px; padding: 10px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; overflow: auto; max-height: 280px; margin: 8px 0 0; line-height: 1.45; }
  pre#logBox { max-height: 340px; min-height: 120px; resize: vertical; }
  .chip-parts { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
  .chk { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text); margin: 6px 0 0; font-weight: 400; }
  .chk input { width: auto; }
  @media (max-width: 880px) { main { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<header>
  <h1><span data-i18n="appTitle">LEAPER-3C ROMライター UI</span> <span class="subver" data-i18n="subver">(macOS版)</span></h1>
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
  </div>
</header>
<main>
  <div>
    <div class="card">
      <h2><span class="step-pill">STEP 1</span><span data-i18n="sec1">プログラマ接続 ＆ ターゲットIC選択</span></h2>
      <div class="hw-bar">
        <span id="hwBadge" class="badge" data-i18n="badgeChecking">USB接続を確認中...</span>
        <button id="btnRescan" onclick="refreshStatus(false)" data-i18n="btnRescan">再検索</button>
      </div>
      <div id="hwMeta" class="errbox" data-i18n="hwChecking">接続確認中...</div>
      <label for="chipFilter" data-i18n="lblChipFilter">IC 検索（型番・メーカー）</label>
      <input id="chipFilter" placeholder="例: 27C256, 27C64, 39SF010A" oninput="filterChips()" />
      <label for="chipSelect" data-i18n="lblChipSelect">ターゲット IC</label>
      <select id="chipSelect" onchange="onSelectChip()"></select>
      <div id="chipMeta" class="kv" style="margin-top:8px;">-</div>
    </div>
    <div class="card">
      <h2><span class="step-pill">STEP 2</span><span data-i18n="sec2">スレーブソケット挿し方ガイド</span></h2>
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
      <h2><span class="step-pill">STEP 3</span><span data-i18n="sec3">ROM / バイナリファイル読込（.bin / .nes）</span></h2>
      <div id="dropzone" class="dropzone" onclick="document.getElementById('fileInput').click()">
        <div data-i18n-html="dropHtml">ここに <code>.bin</code> または <code>.nes</code> をドロップ / クリックして選択</div>
        <div class="dropsub" data-i18n-html="dropSub"><strong>✓ .nes は読込時に iNES / NES 2.0 ヘッダ（16B 等）を自動削除</strong>し、PRG / CHR に自動分割します</div>
        <div class="dropsub" data-i18n="dropAnywhere">（画面上のどこにドロップしても読み込めます）</div>
        <input id="fileInput" type="file" style="display:none" onchange="handleFiles(this.files)" />
      </div>
      <div id="nesNotice" class="infobox" style="display:none;"></div>
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
      <h2><span class="step-pill">STEP 4</span><span data-i18n="sec4">デバイス操作</span></h2>
      <div id="opsOfflineBanner" class="errbox" style="display:none;margin-bottom:10px;" data-i18n="opsOfflineMsg">⚠ ライター未接続のためデバイス操作は無効です（ファイル読込・.nes分割・挿し方図の確認はできます）。USBと12V電源を入れて「再検索」を押してください。</div>
      <div class="row">
        <button data-hw-btn="1" disabled onclick="runAction('blank-check')" data-i18n="btnBlank">ブランクチェック</button>
        <button data-hw-btn="1" disabled onclick="runAction('read')" data-i18n="btnRead">ICから読出し</button>
        <button data-hw-btn="1" disabled onclick="runAction('verify')" data-i18n="btnVerify">ベリファイ（照合）</button>
        <button data-hw-btn="1" disabled id="btnErase" class="danger" onclick="runAction('erase')" data-i18n="btnErase">消去（イレース）</button>
        <button data-hw-btn="1" disabled class="primary" onclick="runAction('program')" data-i18n="btnProgram">書込み（プログラム）</button>
        <button id="btnDownload" disabled onclick="downloadLast()" data-i18n="btnDownload">読出し結果を保存 (.bin)</button>
      </div>
      <div class="progress-wrap"><div id="progBar" class="progress-bar"></div></div>
      <div class="row" style="justify-content:space-between;margin-top:4px;">
        <div id="progText" class="kv" data-i18n="progIdle">待機中</div>
        <div class="row">
          <button class="btn-xs" onclick="scrollLogBottom()" data-i18n="btnLogBottom">最新ログへ</button>
          <button class="btn-xs" onclick="clearLogs()" data-i18n="btnLogClear">ログ消去</button>
        </div>
      </div>
      <pre id="logBox">準備完了。</pre>
    </div>
    <div class="card">
      <h2><span class="step-pill">STEP 5</span><span data-i18n="sec5">HEX ダンプ表示</span></h2>
      <pre id="hexBox" data-i18n="hexEmpty">（データ未読込）</pre>
    </div>
  </div>
</main>
<script>
const I18N = {
  ja: {
    appTitle: "LEAPER-3C ROMライター UI",
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
    sec1: "プログラマ接続 ＆ ターゲットIC選択",
    hwChecking: "接続確認中...",
    opsOfflineMsg: "⚠ ライター未接続のためデバイス操作は無効です（ファイル読込・.nes分割・挿し方図の確認はできます）。USBと12V電源を入れて STEP 1 の「再検索」を押してください。",
    lblChipFilter: "IC 検索（型番・メーカー）",
    chipFilterPh: "例: 27C256, 27C64, 39SF010A",
    lblChipSelect: "ターゲット IC",
    sec2: "スレーブソケット挿し方ガイド",
    warnHtml: "<strong>注意:</strong><br/>• <strong>DC 12V ACアダプタ</strong> 接続＆電源ON必須<br/>• 挿すのは <strong>右側「SLAVE」ソケット</strong><br/>• <strong>切り欠き（1番ピン）は上（LCD側）</strong>、本体位置は <strong>下端（レバー側）揃え</strong>（28ピンICは上側2ピン空き）",
    sec3: "ROM / バイナリファイル読込（.bin / .nes）",
    dropHtml: "ここに <code>.bin</code> または <code>.nes</code> をドロップ / クリックして選択",
    dropSub: "<strong>✓ .nes は読込時に iNES / NES 2.0 ヘッダ（16B 等）を自動削除</strong>し、PRG / CHR に自動分割します",
    dropAnywhere: "（画面上のどこにドロップしても読み込めます）",
    bufEmpty: "ファイル未読込（読出しのみでも実行できます）",
    lblPadMode: "サイズ不一致時の余白埋め",
    padNone: "しない（IC容量と一致必須）",
    padMirror: "ミラー繰り返し（8KB→32KB等）",
    padFf: "0xFF で余白を埋める",
    chkErase: "書込み前に消去（Flash専用）",
    chkVerify: "書込み後にベリファイ（照合）",
    sec4: "デバイス操作",
    btnBlank: "ブランクチェック",
    btnRead: "ICから読出し",
    btnVerify: "ベリファイ（照合）",
    btnErase: "消去（イレース）",
    btnProgram: "書込み（プログラム）",
    btnDownload: "読出し結果を保存 (.bin)",
    btnLogBottom: "最新ログへ",
    btnLogClear: "ログ消去",
    progIdle: "待機中",
    logReady: "準備完了。",
    sec5: "HEX ダンプ表示",
    hexEmpty: "（データ未読込）",
    connectedBadge: fw => `接続済み (FW ${fw})`,
    disconnectedBadge: "未接続",
    connErrBadge: "接続エラー",
    notFound: "LEAPER-3C が見つかりません",
    hwMetaHtml: st => `<strong>✓ 接続済み — ${st.banner} (FW ${st.firmware})</strong><br/>USB: ${st.location} | 対応IC: ${st.chip_count}種 | ステータス 0x42: <code>${st.status_hex}</code>`,
    hwOfflineHtml: msg => `<strong>⚠ ライター未接続</strong><br/>${msg}`,
    chipMetaHtml: info => {
      const base = `容量: <strong>${info.size_bytes.toLocaleString()} B (${info.size_bytes/1024} KB)</strong> | 種類: <strong>${info.is_uv_eprom ? "UV-EPROM (27C系)" : "Flash / EEPROM"}</strong>`;
      return info.source === "offline"
        ? `<em>（未接続・参考一覧）</em> ${base}<br/>接続後に「再検索」で260種＆Vcc/Vppを取得します`
        : `${base} | ~Vcc ${info.vcc} / ~Vpp ${info.vpp}`;
    },
    svgTop: "↑ LCD側 (1番ピン/切り欠き上)",
    svg28: "上2ピン空き・下詰め",
    svg32: "ソケット全体を使用",
    svgBottom: "↓ レバー側 (底辺を揃える)",
    nesHeaderStripped: info => `<strong>✓ ${info.nes_format} ヘッダ（${info.header_bytes} バイト${info.trainer_bytes ? " / Trainer 512B 含む" : ""}）を発見したため自動で削除しました！</strong><br/>純粋な ROM データのみ抽出済み（Mapper ${info.mapper} / PRG ${(info.prg_size/1024)} KB${info.chr_size ? ` + CHR ${(info.chr_size/1024)} KB` : ""}）— 下のボタンから焼きたいバンクを選んでください:`,
    binNoHeader: info => `生バイナリ読込: <strong>${info.filename}</strong>（iNES ヘッダなし・そのまま ${(info.size/1024)} KB をセット済み）`,
    nesBtn: p => `${p.kind} をセット (${p.size/1024} KB / ${p.crc32})`,
    bufLoaded: p => `選択中: <strong>${p.name}</strong> (${p.size.toLocaleString()} bytes) | CRC32=<code>${p.crc32}</code> | SUM16=<code>${p.sum16}</code>`,
    alertNoChip: "IC を選択してください。",
    confirmAction: (act, c) => `${act.toUpperCase()} を実行しますか？\nIC: ${c.vendor} ${c.name}\n(右側SLAVEソケット・下詰め・12V電源を確認してください)`
  },
  en: {
    appTitle: "LEAPER-3C ROM Programmer UI",
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
    sec1: "Programmer Connection & Target IC",
    hwChecking: "Checking connection...",
    opsOfflineMsg: "⚠ Programmer is disconnected — hardware operations are disabled (file loading, .nes splitting, and socket guide still work). Connect USB + 12V power and click Rescan in STEP 1.",
    lblChipFilter: "Search IC (Part / Vendor)",
    chipFilterPh: "e.g. 27C256, 27C64, 39SF010A",
    lblChipSelect: "Target IC",
    sec2: "Slave Socket Placement Guide",
    warnHtml: "<strong>Notice:</strong><br/>• <strong>DC 12V AC Adapter</strong> & Power ON required<br/>• Use the <strong>right-side SLAVE socket</strong><br/>• <strong>Notch (Pin 1) faces UP (LCD)</strong>, aligned to <strong>bottom (lever)</strong> (28-pin leaves top 2 pins empty)",
    sec3: "Load ROM / Binary File (.bin / .nes)",
    dropHtml: "Drop <code>.bin</code> or <code>.nes</code> here, or click to select",
    dropSub: "<strong>✓ Auto-strips iNES / NES 2.0 headers (16B, etc.) on load</strong> & splits into PRG / CHR",
    dropAnywhere: "(You can also drop files anywhere on this page)",
    bufEmpty: "No file loaded (Read can run without a file)",
    lblPadMode: "Padding on size mismatch",
    padNone: "None (exact IC size required)",
    padMirror: "Mirror repeat (e.g. 8KB → 32KB)",
    padFf: "Pad remaining space with 0xFF",
    chkErase: "Erase before write (Flash only)",
    chkVerify: "Verify after write",
    sec4: "Device Operations",
    btnBlank: "Blank Check",
    btnRead: "Read IC",
    btnVerify: "Verify",
    btnErase: "Erase",
    btnProgram: "Program (Write)",
    btnDownload: "Save Readback (.bin)",
    btnLogBottom: "Jump to Latest",
    btnLogClear: "Clear Log",
    progIdle: "Idle",
    logReady: "Ready.",
    sec5: "HEX Dump",
    hexEmpty: "(No data loaded)",
    connectedBadge: fw => `Connected (FW ${fw})`,
    disconnectedBadge: "Disconnected",
    connErrBadge: "Connection Error",
    notFound: "LEAPER-3C not found",
    hwMetaHtml: st => `<strong>✓ Connected — ${st.banner} (FW ${st.firmware})</strong><br/>USB: ${st.location} | Chips: ${st.chip_count} | Status 0x42: <code>${st.status_hex}</code>`,
    hwOfflineHtml: msg => `<strong>⚠ Programmer Disconnected</strong><br/>${msg}`,
    chipMetaHtml: info => {
      const base = `Size: <strong>${info.size_bytes.toLocaleString()} B (${info.size_bytes/1024} KB)</strong> | Type: <strong>${info.is_uv_eprom ? "UV-EPROM (27C)" : "Flash / EEPROM"}</strong>`;
      return info.source === "offline"
        ? `<em>(Offline reference)</em> ${base}<br/>Connect hardware & click Rescan for all 260 ICs & Vcc/Vpp`
        : `${base} | ~Vcc ${info.vcc} / ~Vpp ${info.vpp}`;
    },
    svgTop: "↑ LCD Side (Pin 1 / Notch UP)",
    svg28: "Top 2 pins empty / Bottom-aligned",
    svg32: "Uses full 32-pin socket",
    svgBottom: "↓ Lever Side (Align bottom edge)",
    nesHeaderStripped: info => `<strong>✓ ${info.nes_format} header (${info.header_bytes} bytes${info.trainer_bytes ? " incl. 512B trainer" : ""}) detected and automatically stripped!</strong><br/>Raw ROM extracted (Mapper ${info.mapper} / PRG ${(info.prg_size/1024)} KB${info.chr_size ? ` + CHR ${(info.chr_size/1024)} KB` : ""}) — choose a bank below:`,
    binNoHeader: info => `Raw binary loaded: <strong>${info.filename}</strong> (no iNES header; ${(info.size/1024)} KB loaded as-is)`,
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
let lastInspectInfo = null;
let lastLogs = [];

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
  if (lastStatusData) {
    renderStatusData(lastStatusData);
    if (!pollTimer) refreshStatus(true);
  }
  if (lastInspectInfo) renderInspectNotice(lastInspectInfo);
  if (currentChip) {
    document.getElementById("chipMeta").innerHTML = t("chipMetaHtml")(currentChip);
    drawSocketSvg(currentChip.pin_count);
  } else {
    drawSocketSvg(28);
  }
  updateLogView(lastLogs, false);
}

function renderInspectNotice(info) {
  const notice = document.getElementById("nesNotice");
  if (!notice || !info) return;
  notice.style.display = "block";
  if (info.is_nes) {
    notice.className = "infobox";
    notice.innerHTML = t("nesHeaderStripped")(info);
  } else {
    notice.className = "infobox plain";
    notice.innerHTML = t("binNoHeader")(info);
  }
}

let allChips = [];
let chipSource = "none";
let hwConnected = false;
let currentChip = null;
let loadedBuffer = { name: "", b64: "", size: 0 };
let pollTimer = null;

function updateHwButtons() {
  document.querySelectorAll("[data-hw-btn]").forEach(btn => {
    if (!hwConnected) {
      btn.disabled = true;
    } else if (btn.id === "btnErase") {
      btn.disabled = Boolean(currentChip && currentChip.is_uv_eprom);
    } else {
      btn.disabled = false;
    }
  });
  const opsBanner = document.getElementById("opsOfflineBanner");
  if (opsBanner) opsBanner.style.display = hwConnected ? "none" : "block";
  const rescanBtn = document.getElementById("btnRescan");
  if (rescanBtn) rescanBtn.className = hwConnected ? "btn-xs" : "primary";
}

async function api(url, body) {
  const headers = { "X-Ui-Lang": curLang };
  if (body) headers["Content-Type"] = "application/json";
  const res = await fetch(url, body ? {
    method: "POST",
    headers,
    body: JSON.stringify(body)
  } : { headers });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function renderStatusData(st) {
  const badge = document.getElementById("hwBadge");
  const meta = document.getElementById("hwMeta");
  hwConnected = Boolean(st.connected);
  if (st.connected) {
    badge.className = "badge ok";
    badge.textContent = t("connectedBadge")(st.firmware);
    meta.className = "infobox";
    meta.style.marginTop = "0";
    meta.style.marginBottom = "8px";
    meta.innerHTML = t("hwMetaHtml")(st);
  } else {
    badge.className = "badge err";
    badge.textContent = t("disconnectedBadge");
    meta.className = "errbox";
    meta.innerHTML = t("hwOfflineHtml")(st.error || t("notFound"));
  }
  updateHwButtons();
}

let statusChecking = false;

async function refreshStatus(quick = false) {
  if (statusChecking) return;
  statusChecking = true;
  const badge = document.getElementById("hwBadge");
  const meta = document.getElementById("hwMeta");
  try {
    const st = await api(quick ? "/api/status?quick=1" : "/api/status");
    if (st.busy) return;
    const prevConnected = hwConnected;
    lastStatusData = st;
    renderStatusData(st);
    if ((st.connected && chipSource !== "hardware") || (!st.connected && prevConnected) || allChips.length === 0) {
      await loadChips();
    }
  } catch (e) {
    hwConnected = false;
    badge.className = "badge err";
    badge.textContent = t("connErrBadge");
    meta.className = "errbox";
    meta.innerHTML = t("hwOfflineHtml")(e.message);
    updateHwButtons();
    if (allChips.length === 0) await loadChips().catch(() => {});
  } finally {
    statusChecking = false;
  }
}

async function loadChips() {
  const data = await api("/api/chips");
  allChips = data.chips || [];
  chipSource = data.source || "offline";
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
    updateHwButtons();
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
dz.addEventListener("drop", e => { e.preventDefault(); e.stopPropagation(); dz.classList.remove("drag"); handleFiles(e.dataTransfer.files); });
window.addEventListener("dragover", e => { e.preventDefault(); dz.classList.add("drag"); });
window.addEventListener("dragleave", e => { if (!e.relatedTarget) dz.classList.remove("drag"); });
window.addEventListener("drop", e => {
  e.preventDefault();
  dz.classList.remove("drag");
  if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length > 0) {
    handleFiles(e.dataTransfer.files);
  }
});

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
      lastInspectInfo = info;
      renderInspectNotice(info);
      const partsDiv = document.getElementById("nesParts");
      partsDiv.innerHTML = "";
      if (info.is_nes) {
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
      await syncLogs(false);
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

function updateLogView(logs, forceBottom = false) {
  lastLogs = logs || [];
  const lb = document.getElementById("logBox");
  if (!lb) return;
  const wasAtBottom = (lb.scrollHeight - lb.scrollTop - lb.clientHeight) <= 28;
  const prevTop = lb.scrollTop;
  const text = lastLogs.length > 0 ? lastLogs.join("\n") : t("logReady");
  if (lb.textContent !== text) {
    lb.textContent = text;
  }
  if (forceBottom || wasAtBottom) {
    lb.scrollTop = lb.scrollHeight;
  } else {
    lb.scrollTop = prevTop;
  }
}

function scrollLogBottom() {
  const lb = document.getElementById("logBox");
  if (lb) lb.scrollTop = lb.scrollHeight;
}

async function clearLogs() {
  try {
    await api("/api/clear-logs", {});
    updateLogView([], true);
  } catch (e) {
    alert(e.message);
  }
}

async function syncLogs(forceBottom = false) {
  try {
    const j = await api("/api/job");
    updateLogView(j.logs || [], forceBottom);
  } catch (_) {}
}

async function pollJob() {
  const j = await api("/api/job");
  document.getElementById("progBar").style.width = `${j.pct}%`;
  document.getElementById("progText").textContent = `${j.phase} (${j.done}/${j.total} - ${j.pct}%)` +
    (j.result ? ` — ${j.result.message}` : "") + (j.error ? ` — ERROR: ${j.error}` : "");
  updateLogView(j.logs || [], false);
  if (!j.running) {
    clearInterval(pollTimer);
    pollTimer = null;
    if (j.result && j.result.hex_preview) {
      document.getElementById("hexBox").textContent = j.result.hex_preview;
    }
    if (j.result && j.result.download_ready) {
      document.getElementById("btnDownload").disabled = false;
    }
    if (j.error) {
      refreshStatus(true);
    }
  }
}

function downloadLast() {
  window.location.href = "/api/download-last";
}

setTheme(curTheme);
setLanguage(curLang);
drawSocketSvg(28);
updateHwButtons();
refreshStatus(false);
syncLogs(true);
setInterval(() => {
  if (!pollTimer && !statusChecking) refreshStatus(true);
}, 2000);
window.addEventListener("focus", () => {
  if (!pollTimer && !statusChecking) refreshStatus(true);
});
</script>
</body>
</html>
"""


def make_handler(state: UiState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _ui_lang(self) -> str:
            return _norm_lang(self.headers.get("X-Ui-Lang", "ja"))

        def _send_json(self, obj: Any, status: int = HTTPStatus.OK) -> None:
            raw = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
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
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if self.path == "/api/status" or self.path.startswith("/api/status?"):
                quick = "quick=1" in self.path
                self._send_json(state.get_status(quick=quick, lang=self._ui_lang()))
                return
            if self.path == "/api/chips":
                try:
                    chips, source = state.get_chips()
                    self._send_json({"chips": chips, "source": source})
                except Exception as exc:
                    self._send_json({"error": _localize_error(str(exc), self._ui_lang())}, HTTPStatus.BAD_REQUEST)
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
            lang = self._ui_lang()
            try:
                body = self._read_json()
                if self.path == "/api/chip-info":
                    self._send_json(state.get_chip_info(body.get("name", ""), body.get("vendor", ""), lang=lang))
                    return
                if self.path == "/api/inspect-file":
                    raw = base64.b64decode(body.get("data_b64", ""))
                    self._send_json(state.inspect_uploaded_file(body.get("filename", "rom.bin"), raw, lang=lang))
                    return
                if self.path == "/api/action":
                    jid = state.start_action(body, lang=lang)
                    self._send_json({"job_id": jid})
                    return
                if self.path == "/api/clear-logs":
                    state.clear_logs()
                    self._send_json({"ok": True})
                    return
                self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            except Exception as exc:
                self._send_json({"error": _localize_error(str(exc), lang)}, HTTPStatus.BAD_REQUEST)

    return Handler


def serve_ui(port: int = 8765, open_browser: bool = True) -> int:
    state = UiState()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    except OSError as exc:
        print(
            f"[ERROR] ポート {port} はすでに起動中の別の Web UI が使用中です ({exc})。\n"
            f"最新版を反映するには古いプロセスを停止してから再起動してください:\n"
            f"  pkill -f 'leaper3c.py ui' ; leaper3c ui",
            flush=True,
        )
        return 1
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

