#!/usr/bin/env python3
"""
LEAPER-3C (LP-3C) Flash / EPROM Writer - macOS Native USB Driver & CLI Tool

Independent open-source user-space libusb/PyUSB host implementation for
interoperability with the LEAP Electronic LEAPER-3C programmer
(VID=0x04CE, PID=0x0A3C, LP-3C Firmware 1.30) without Windows or kernel drivers.
Contains no proprietary vendor software, drivers, or firmware binaries.
"""

from __future__ import annotations

import argparse
import hashlib
import struct
import sys
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol

try:
    import usb.backend.libusb1
    import usb.core
    import usb.util
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "pyusb が見つかりません。`pip install -r requirements.txt` で依存関係をインストールしてください。"
    ) from exc


LP3C_VID = 0x04CE
LP3C_PID = 0x0A3C
EP_BULK_IN = 0x81
EP_BULK_OUT = 0x02

BLOCK_SIZE = 512
PROG_CHUNK_SIZE = 128
READ_CHUNK_SIZE = 64
BUFFER_BASE_ADDR = 0x0800

VENDOR_TABLE_ADDR = 0xDEE8
VENDOR_COUNT = 19
PREFIX_TABLE_ADDR = 0xDE68
PREFIX_COUNT = 32

# LP-3C 0x49 Subcommands (passed in wIndex)
CMD_SESSION_START = 0x50   # 'P' - Read / BlankCheck / Erase / Verify session
CMD_SESSION_END = 0x52     # 'R' - End session & release
CMD_PROG_START = 0x54      # 'T' - Program session start
CMD_READ_BLOCK = 0x57      # 'W' - Read next 512B block into buffer 0x0800
CMD_BLANK_CHECK = 0x59     # 'Y' - Blank check device
CMD_PROG_BLOCK = 0x5B      # '[' - Program 512B buffer at 0x0800 to device
CMD_ERASE = 0x5C           # '\' - Electrical erase device
CMD_HW_VERIFY = 0x5D       # ']' - Hardware verify


class LeaperError(RuntimeError):
    """Raised when LP-3C communication or chip operation fails."""


class UsbBackend(Protocol):
    """Minimal USB transport abstraction for hardware and unit-test mocking."""

    def ctrl_read(self, req: int, val: int, idx: int, length: int, timeout: int) -> bytes: ...
    def ctrl_write(self, req: int, val: int, idx: int, data: bytes, timeout: int) -> int: ...
    def bulk_write(self, data: bytes, timeout: int) -> int: ...


def _candidate_libusb_paths() -> list[Path]:
    """Return candidate libusb-1.0.dylib paths, preferring bundled .app copies first."""
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        mp = Path(meipass)
        candidates.extend(
            [
                mp / "libusb-1.0.dylib",
                mp / "libusb-1.0.0.dylib",
                mp / "lib" / "libusb-1.0.dylib",
                mp / "lib" / "libusb-1.0.0.dylib",
            ]
        )
    exe_dir = Path(sys.executable).resolve().parent
    candidates.extend(
        [
            exe_dir / "libusb-1.0.dylib",
            exe_dir / "libusb-1.0.0.dylib",
            exe_dir.parent / "Frameworks" / "libusb-1.0.dylib",
            exe_dir.parent / "Frameworks" / "libusb-1.0.0.dylib",
            exe_dir.parent / "Resources" / "libusb-1.0.dylib",
            Path("/opt/homebrew/lib/libusb-1.0.dylib"),
            Path("/usr/local/lib/libusb-1.0.dylib"),
        ]
    )
    return candidates


def _find_libusb_backend():
    """Locate libusb-1.0 explicitly from app bundle or Apple Silicon / Intel Homebrew."""
    for candidate in _candidate_libusb_paths():
        if candidate.exists():
            backend = usb.backend.libusb1.get_backend(find_library=lambda _name, p=str(candidate): p)
            if backend is not None:
                return backend
    backend = usb.backend.libusb1.get_backend()
    if backend is not None:
        return backend
    raise LeaperError(
        "libusb が見つかりません。`brew install libusb` を実行してください。"
    )


class PyUsbBackend:
    """Live libusb/PyUSB transport for LP-3C hardware."""

    def __init__(self, vid: int = LP3C_VID, pid: int = LP3C_PID) -> None:
        backend = _find_libusb_backend()
        dev = usb.core.find(idVendor=vid, idProduct=pid, backend=backend)
        if dev is None:
            raise LeaperError(
                f"LEAPER-3C (VID=0x{vid:04X}, PID=0x{pid:04X}) が見つかりません。"
                "USBケーブルと12V電源スイッチを確認してください。"
            )
        self.dev = dev
        try:
            self.dev.set_configuration()
        except usb.core.USBError:
            # Already configured or OS claimed; claim interface directly below.
            pass
        try:
            usb.util.claim_interface(self.dev, 0)
        except usb.core.USBError as exc:
            raise LeaperError(f"USBインターフェース(0)を確保できませんでした: {exc}") from exc

    @property
    def usb_location(self) -> str:
        return f"bus={self.dev.bus}, addr={self.dev.address}"

    def ctrl_read(self, req: int, val: int, idx: int, length: int, timeout: int = 10000) -> bytes:
        try:
            res = self.dev.ctrl_transfer(0xC0, req, val, idx, length, timeout=timeout)
            return bytes(res)
        except usb.core.USBError as exc:
            raise LeaperError(
                f"USB VendorRead(0x{req:02X}, val=0x{val:04X}, idx=0x{idx:04X}, len={length}) 失敗: {exc}"
            ) from exc

    def ctrl_write(self, req: int, val: int, idx: int, data: bytes = b"", timeout: int = 10000) -> int:
        try:
            return int(self.dev.ctrl_transfer(0x40, req, val, idx, data, timeout=timeout))
        except usb.core.USBError as exc:
            raise LeaperError(
                f"USB VendorWrite(0x{req:02X}, val=0x{val:04X}, idx=0x{idx:04X}, len={len(data)}) 失敗: {exc}"
            ) from exc

    def bulk_write(self, data: bytes, timeout: int = 5000) -> int:
        try:
            return int(self.dev.write(EP_BULK_OUT, data, timeout=timeout))
        except usb.core.USBError as exc:
            raise LeaperError(f"USB BulkWrite(EP 0x{EP_BULK_OUT:02X}, len={len(data)}) 失敗: {exc}") from exc

    def close(self) -> None:
        try:
            usb.util.release_interface(self.dev, 0)
        except Exception:
            pass
        try:
            usb.util.dispose_resources(self.dev)
        except Exception:
            pass


@dataclass(frozen=True)
class ChipEntry:
    vendor_id: int
    vendor_name: str
    dev_id: int
    name: str
    prefix_idx: int
    flags: int

    @property
    def is_uv_eprom(self) -> bool:
        return self.vendor_name == "27XXX" or self.name.startswith("27C")


@dataclass(frozen=True)
class ChipParams:
    raw: bytes
    size_bytes: int
    vcc_raw: int
    vpp_raw: int

    @property
    def blocks_512(self) -> int:
        return (self.size_bytes + BLOCK_SIZE - 1) // BLOCK_SIZE


@dataclass(frozen=True)
class DeviceInfo:
    banner: str
    fw_version: str
    table_base_addr: int
    raw: bytes


def decode_chip_size(param_bytes: bytes) -> int:
    """Decode the 34-byte 0x4B response into chip capacity in bytes (matches 0x405898)."""
    if len(param_bytes) < 32:
        raise LeaperError(f"0x4B パラメータ長が不正です: {len(param_bytes)} bytes")
    b28, b29, b30, b31 = param_bytes[28], param_bytes[29], param_bytes[30], param_bytes[31]
    hi = b28 if b29 == 0 else (b29 << (b28 + 8))
    lo = b30 if b31 == 0 else (b31 << (b30 + 8))
    return (hi << 16) + lo


def parse_chip_table(blob: bytes, base_addr: int) -> list[ChipEntry]:
    """Parse built-in vendor, prefix, and IC records read from LP-3C RAM."""

    def at(addr: int) -> bytes:
        off = addr - base_addr
        if off < 0 or off >= len(blob):
            raise LeaperError(f"チップテーブル範囲外アドレス: 0x{addr:04X}")
        return blob[off:]

    vendors: list[tuple[int, str]] = []
    for i in range(VENDOR_COUNT):
        rec = at(VENDOR_TABLE_ADDR + i * 8)[:8]
        name = rec[:7].split(b"\x00", 1)[0].decode("ascii", "replace").strip()
        vendors.append((rec[7], name))

    prefixes: list[str] = []
    for i in range(PREFIX_COUNT):
        rec = at(PREFIX_TABLE_ADDR + i * 4)[:4]
        prefixes.append(rec.split(b"\x00", 1)[0].decode("ascii", "replace").strip())

    chips: list[ChipEntry] = []
    off = 0
    v_idx = 0
    d_idx = 0
    end_addr = PREFIX_TABLE_ADDR
    while base_addr + off < end_addr and v_idx < len(vendors):
        rec = blob[off : off + 8]
        if len(rec) < 2:
            break
        if rec[:2] == b"\xff\xff":
            v_idx += 1
            d_idx = 0
            off += 2
            continue
        if len(rec) < 8:
            break
        pidx = rec[0]
        suffix = rec[1:7].split(b"\x00", 1)[0].decode("ascii", "replace").strip()
        flags = rec[7]
        pref = prefixes[pidx] if pidx < len(prefixes) else f"?{pidx}"
        v_id, v_name = vendors[v_idx]
        chips.append(
            ChipEntry(
                vendor_id=v_id,
                vendor_name=v_name,
                dev_id=d_idx,
                name=f"{pref}{suffix}",
                prefix_idx=pidx,
                flags=flags,
            )
        )
        d_idx += 1
        off += 8

    return chips


def check_status_bytes(status: bytes, action: str) -> None:
    """Interpret the 8-byte 0x42 status response (matches erase/check/program handlers)."""
    if len(status) < 2:
        raise LeaperError(f"{action}: 0x42 ステータス長が不正です ({len(status)} bytes)")
    b0, b1 = status[0], status[1]
    if b1 == 1:
        raise LeaperError(f"{action}: RUN FAIL (status={status.hex(' ')}) — IC向き・接触・12V電源を確認してください")
    if b1 == 8:
        raise LeaperError(f"{action}: OVER CURRENT FAIL (status={status.hex(' ')}) — 過電流を検出しました！ICを逆挿ししていないか直ちに確認してください")
    if b1 != 0:
        raise LeaperError(f"{action}: エラーコード status[1]=0x{b1:02X} (status={status.hex(' ')})")
    if b0 != 0:
        raise LeaperError(f"{action}: 失敗 status[0]=0x{b0:02X} (status={status.hex(' ')})")


def pad_image_to_chip(data: bytes, target_size: int, pad_mode: str) -> bytes:
    """Prepare ROM binary for target chip size using 'none', 'ff', or 'mirror' padding."""
    if len(data) > target_size:
        raise LeaperError(
            f"入力ファイルサイズ ({len(data)} bytes) が選択IC容量 ({target_size} bytes) を超えています。"
        )
    if len(data) == target_size:
        return data
    if pad_mode == "none":
        raise LeaperError(
            f"入力ファイルサイズ ({len(data)} bytes) がIC容量 ({target_size} bytes) より小さいです。"
            " --pad mirror (ミラー繰り返し) または --pad ff (0xFF埋め) を指定してください。"
        )
    if pad_mode == "ff":
        return data + b"\xff" * (target_size - len(data))
    if pad_mode == "mirror":
        if target_size % len(data) != 0:
            raise LeaperError(
                f"--pad mirror にはIC容量 ({target_size}) がファイルサイズ ({len(data)}) の整数倍である必要があります。"
            )
        return data * (target_size // len(data))
    raise ValueError(f"Unknown pad_mode: {pad_mode}")


def sum16(data: bytes) -> int:
    """16-bit byte sum matching programmer DeviceSum / CheckSUM convention."""
    return sum(data) & 0xFFFFFFFF


class Leaper3C:
    """High-level driver for the LEAPER-3C USB programmer."""

    def __init__(self, backend: UsbBackend) -> None:
        self.usb = backend
        self._chips_cache: Optional[list[ChipEntry]] = None

    def get_info(self) -> DeviceInfo:
        raw = self.usb.ctrl_read(0x41, 0, 0, 0x16, timeout=5000)
        if len(raw) < 22:
            raise LeaperError(f"0x41 応答が短すぎます: {raw!r}")
        text = raw[:20].decode("ascii", "replace").strip()
        parts = text.split()
        fw = parts[-1] if parts else "?"
        base_addr = struct.unpack("<H", raw[20:22])[0]
        return DeviceInfo(banner=text, fw_version=fw, table_base_addr=base_addr, raw=raw)

    def read_status(self, timeout: int = 15000) -> bytes:
        return self.usb.ctrl_read(0x42, 0, 0, 8, timeout=timeout)

    def read_ram(self, addr: int, length: int) -> bytes:
        out = bytearray()
        cur = 0
        while cur < length:
            n = min(READ_CHUNK_SIZE, length - cur)
            chunk = self.usb.ctrl_read(0x44, addr + cur, 0, n, timeout=5000)
            out.extend(chunk[:n])
            cur += n
        return bytes(out)

    def list_chips(self) -> list[ChipEntry]:
        if self._chips_cache is not None:
            return self._chips_cache
        info = self.get_info()
        read_len = (0xDF90 - info.table_base_addr + 0x3F) & ~0x3F
        blob = self.read_ram(info.table_base_addr, read_len)
        self._chips_cache = parse_chip_table(blob, info.table_base_addr)
        return self._chips_cache

    def find_chip(self, chip_name: str, vendor_name: Optional[str] = None) -> ChipEntry:
        chips = self.list_chips()
        q_chip = chip_name.strip().upper()
        q_vendor = vendor_name.strip().upper() if vendor_name else None

        exact = [
            c
            for c in chips
            if c.name.upper() == q_chip and (q_vendor is None or c.vendor_name.upper() == q_vendor)
        ]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            choices = ", ".join(f"{c.vendor_name}:{c.name}" for c in exact)
            raise LeaperError(
                f"複数のメーカーに '{chip_name}' が存在します ({choices})。--vendor でメーカーを指定してください。"
            )

        # Fallback: substring match if unambiguous
        partial = [
            c
            for c in chips
            if q_chip in c.name.upper() and (q_vendor is None or q_vendor in c.vendor_name.upper())
        ]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            choices = ", ".join(f"{c.vendor_name}:{c.name}" for c in partial[:12])
            raise LeaperError(
                f"'{chip_name}' に複数候補があります: {choices}。正確な型番または --vendor を指定してください。"
            )
        raise LeaperError(f"対応ICリストに '{chip_name}' が見つかりません。`list` コマンドで確認してください。")

    def select_chip(self, chip: ChipEntry) -> ChipParams:
        raw = self.usb.ctrl_read(0x4B, chip.vendor_id, chip.dev_id, 0x22, timeout=5000)
        size = decode_chip_size(raw)
        return ChipParams(raw=raw, size_bytes=size, vcc_raw=raw[18], vpp_raw=raw[20])

    def _vendor_dummy(self, vendor_id: int) -> bytes:
        # LP-3C protocol expects vendor_id passed as wLength with an 0xFF filler buffer.
        return b"\xff" * vendor_id if vendor_id > 0 else b""

    def blank_check(self, chip: ChipEntry) -> bytes:
        self.select_chip(chip)
        self.usb.ctrl_write(0x49, 0, CMD_SESSION_START, b"")
        try:
            self.usb.ctrl_write(0x49, chip.dev_id, CMD_BLANK_CHECK, self._vendor_dummy(chip.vendor_id), timeout=30000)
            status = self.read_status(timeout=30000)
            check_status_bytes(status, "Blank Check")
            return status
        finally:
            self.usb.ctrl_write(0x49, 0, CMD_SESSION_END, b"")

    def erase(self, chip: ChipEntry) -> bytes:
        if chip.is_uv_eprom:
            raise LeaperError(
                f"{chip.vendor_name} {chip.name} は紫外線消去型EPROMのため電気消去できません（UVイレーサーが必要です）。"
            )
        self.select_chip(chip)
        self.usb.ctrl_write(0x49, 0, CMD_SESSION_START, b"")
        try:
            self.usb.ctrl_write(0x49, chip.dev_id, CMD_ERASE, self._vendor_dummy(chip.vendor_id), timeout=30000)
            status = self.read_status(timeout=45000)
            check_status_bytes(status, "Erase")
            return status
        finally:
            self.usb.ctrl_write(0x49, 0, CMD_SESSION_END, b"")

    def read_chip(
        self,
        chip: ChipEntry,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> bytes:
        params = self.select_chip(chip)
        total_blocks = params.blocks_512
        dummy = self._vendor_dummy(chip.vendor_id)
        out = bytearray()

        self.usb.ctrl_write(0x49, 0, CMD_SESSION_START, b"")
        try:
            for blk in range(total_blocks):
                self.usb.ctrl_write(0x49, chip.dev_id, CMD_READ_BLOCK, dummy, timeout=10000)
                for chunk_idx in range(BLOCK_SIZE // READ_CHUNK_SIZE):
                    out.extend(
                        self.usb.ctrl_read(
                            0x44,
                            BUFFER_BASE_ADDR + chunk_idx * READ_CHUNK_SIZE,
                            0,
                            READ_CHUNK_SIZE,
                            timeout=5000,
                        )
                    )
                if progress_cb:
                    progress_cb(blk + 1, total_blocks)
        finally:
            self.usb.ctrl_write(0x49, 0, CMD_SESSION_END, b"")

        return bytes(out[: params.size_bytes])

    def program_chip(
        self,
        chip: ChipEntry,
        image: bytes,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        params = self.select_chip(chip)
        if len(image) != params.size_bytes:
            raise LeaperError(
                f"書き込みデータサイズ ({len(image)} bytes) が {chip.name} の容量 ({params.size_bytes} bytes) と一致しません。"
            )
        total_blocks = params.blocks_512

        # LP-3C sequence: 0x4B(vendor_id, dev_id) selects the active chip,
        # then starts program mode with 0x49 'T' (0x54), uploads 4 x 128B chunks via 0x47 + BulkWrite,
        # commits each 512B block with 0x49 '[' (0x5B), polls 0x42, and ends with 0x49 'R' (0x52).
        self.usb.ctrl_write(0x49, 0, CMD_PROG_START, b"")
        try:
            for blk in range(total_blocks):
                block_data = image[blk * BLOCK_SIZE : (blk + 1) * BLOCK_SIZE]
                for chunk_idx in range(BLOCK_SIZE // PROG_CHUNK_SIZE):
                    chunk = block_data[chunk_idx * PROG_CHUNK_SIZE : (chunk_idx + 1) * PROG_CHUNK_SIZE]
                    self.usb.ctrl_write(0x47, BUFFER_BASE_ADDR + chunk_idx * PROG_CHUNK_SIZE, PROG_CHUNK_SIZE, b"")
                    time.sleep(0.02)
                    self.usb.bulk_write(chunk, timeout=5000)
                    time.sleep(0.02)

                self.usb.ctrl_write(0x49, 0, CMD_PROG_BLOCK, b"", timeout=30000)
                time.sleep(0.01)
                status = self.read_status(timeout=30000)
                check_status_bytes(status, f"Program block {blk}/{total_blocks} (addr=0x{blk * BLOCK_SIZE:05X})")
                if progress_cb:
                    progress_cb(blk + 1, total_blocks)
        finally:
            self.usb.ctrl_write(0x49, 0, CMD_SESSION_END, b"")


def split_ines_rom(
    nes_path: Path,
    out_dir: Path,
    prg_pad_kb: Optional[int] = None,
    chr_pad_kb: Optional[int] = None,
) -> dict[str, object]:
    """Extract PRG and CHR binaries from an iNES / NES 2.0 .nes ROM file."""
    raw = nes_path.read_bytes()
    if len(raw) < 16 or raw[:4] != b"NES\x1a":
        raise LeaperError(f"有効な iNES (.nes) ヘッダではありません: {nes_path}")

    prg_banks = raw[4]
    chr_banks = raw[5]
    flags6 = raw[6]
    flags7 = raw[7]
    is_nes2 = (flags7 & 0x0C) == 0x08
    if is_nes2:
        prg_msb = raw[9] & 0x0F
        chr_msb = (raw[9] >> 4) & 0x0F
        prg_banks |= prg_msb << 8
        chr_banks |= chr_msb << 8

    mapper = ((flags6 >> 4) & 0x0F) | (flags7 & 0xF0)
    has_trainer = bool(flags6 & 0x04)
    offset = 16 + (512 if has_trainer else 0)
    prg_size = prg_banks * 16384
    chr_size = chr_banks * 8192

    if len(raw) < offset + prg_size + chr_size:
        raise LeaperError(
            f".nes ファイル長が不足しています: expected>={offset + prg_size + chr_size}, actual={len(raw)}"
        )

    prg_data = raw[offset : offset + prg_size]
    chr_data = raw[offset + prg_size : offset + prg_size + chr_size]

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = nes_path.stem
    prg_file = out_dir / f"{stem}.prg.bin"
    prg_file.write_bytes(prg_data)

    chr_file: Optional[Path] = None
    if chr_size > 0:
        chr_file = out_dir / f"{stem}.chr.bin"
        chr_file.write_bytes(chr_data)

    prg_padded_file: Optional[Path] = None
    if prg_pad_kb:
        padded = pad_image_to_chip(prg_data, prg_pad_kb * 1024, "mirror")
        prg_padded_file = out_dir / f"{stem}.prg.{prg_pad_kb}k.bin"
        prg_padded_file.write_bytes(padded)

    chr_padded_file: Optional[Path] = None
    if chr_pad_kb and chr_size > 0:
        padded = pad_image_to_chip(chr_data, chr_pad_kb * 1024, "mirror")
        chr_padded_file = out_dir / f"{stem}.chr.{chr_pad_kb}k.bin"
        chr_padded_file.write_bytes(padded)

    return {
        "mapper": mapper,
        "prg_size": prg_size,
        "chr_size": chr_size,
        "prg_file": prg_file,
        "chr_file": chr_file,
        "prg_padded_file": prg_padded_file,
        "chr_padded_file": chr_padded_file,
        "prg_crc32": zlib.crc32(prg_data) & 0xFFFFFFFF,
        "chr_crc32": zlib.crc32(chr_data) & 0xFFFFFFFF if chr_size > 0 else 0,
    }


def verify_buffers(expected: bytes, actual: bytes) -> Optional[tuple[int, int, int]]:
    """Return None if identical, else (first_mismatch_offset, expected_byte, actual_byte)."""
    if expected == actual:
        return None
    limit = min(len(expected), len(actual))
    for i in range(limit):
        if expected[i] != actual[i]:
            return (i, expected[i], actual[i])
    return (limit, expected[limit] if len(expected) > limit else -1, actual[limit] if len(actual) > limit else -1)


def _make_progress_cb(label: str) -> Callable[[int, int], None]:
    def cb(done: int, total: int) -> None:
        pct = int(done * 100 / total) if total else 100
        bar_w = 24
        filled = int(bar_w * done / total) if total else bar_w
        bar = "#" * filled + "-" * (bar_w - filled)
        sys.stdout.write(f"\r{label}: [{bar}] {done}/{total} ({pct}%)")
        sys.stdout.flush()
        if done >= total:
            sys.stdout.write("\n")

    return cb


def _open_programmer() -> tuple[PyUsbBackend, Leaper3C]:
    backend = PyUsbBackend()
    return backend, Leaper3C(backend)


def cmd_info(_args: argparse.Namespace) -> int:
    backend, lp = _open_programmer()
    try:
        info = lp.get_info()
        st = lp.read_status(timeout=5000)
        chips = lp.list_chips()
        print(f"Device      : LEAPER-3C ({backend.usb_location})")
        print(f"USB ID      : 0x{LP3C_VID:04X}:0x{LP3C_PID:04X}")
        print(f"Banner      : {info.banner}")
        print(f"Firmware    : {info.fw_version}")
        print(f"IC DB Addr  : 0x{info.table_base_addr:04X} ({len(chips)} ICs)")
        print(f"Status 0x42 : {st.hex(' ')}")
        return 0
    finally:
        backend.close()


def cmd_list(args: argparse.Namespace) -> int:
    backend, lp = _open_programmer()
    try:
        chips = lp.list_chips()
        vf = args.vendor.upper() if args.vendor else None
        kf = args.query.upper() if args.query else None
        shown = 0
        for c in chips:
            if vf and vf not in c.vendor_name.upper():
                continue
            if kf and kf not in c.name.upper() and kf not in c.vendor_name.upper():
                continue
            print(f"{c.vendor_name:<8} {c.name:<12} (vendor_id={c.vendor_id:2d}, dev_id={c.dev_id:2d})")
            shown += 1
        print(f"Total matched: {shown} / {len(chips)}")
        return 0
    finally:
        backend.close()


def cmd_chip_info(args: argparse.Namespace) -> int:
    backend, lp = _open_programmer()
    try:
        chip = lp.find_chip(args.chip, args.vendor)
        params = lp.select_chip(chip)
        print(f"Vendor      : {chip.vendor_name} (id={chip.vendor_id})")
        print(f"Chip        : {chip.name} (dev_id={chip.dev_id}, flags=0x{chip.flags:02X})")
        print(f"Type        : {'UV EPROM (27C系)' if chip.is_uv_eprom else 'Flash / EEPROM'}")
        print(f"Capacity    : {params.size_bytes} bytes ({params.size_bytes // 1024} KB / {params.blocks_512} x 512B blocks)")
        print(f"Vcc / Vpp   : ~{params.vcc_raw / 10:.1f}V / ~{params.vpp_raw / 10:.1f}V (raw {params.vcc_raw}, {params.vpp_raw})")
        print(f"Raw 0x4B    : {params.raw.hex(' ')}")
        return 0
    finally:
        backend.close()


def cmd_blank_check(args: argparse.Namespace) -> int:
    backend, lp = _open_programmer()
    try:
        chip = lp.find_chip(args.chip, args.vendor)
        params = lp.select_chip(chip)
        print(f"Blank Check : {chip.vendor_name} {chip.name} ({params.size_bytes // 1024} KB)...")
        st = lp.blank_check(chip)
        print(f"PASS (Blank / all 0xFF)  status={st.hex(' ')}")
        print("※注意: テストソケット(Slave Textool)にICを正しく挿入しレバーを倒した状態で確認してください（空ソケットでも0xFF読出しでPASSになります）。")
        return 0
    finally:
        backend.close()


def cmd_erase(args: argparse.Namespace) -> int:
    backend, lp = _open_programmer()
    try:
        chip = lp.find_chip(args.chip, args.vendor)
        params = lp.select_chip(chip)
        print(f"Erase       : {chip.vendor_name} {chip.name} ({params.size_bytes // 1024} KB)...")
        st = lp.erase(chip)
        print(f"PASS (Erased)  status={st.hex(' ')}")
        return 0
    finally:
        backend.close()


def cmd_read(args: argparse.Namespace) -> int:
    backend, lp = _open_programmer()
    try:
        chip = lp.find_chip(args.chip, args.vendor)
        params = lp.select_chip(chip)
        print(f"Reading     : {chip.vendor_name} {chip.name} ({params.size_bytes} bytes)...")
        t0 = time.time()
        data = lp.read_chip(chip, progress_cb=_make_progress_cb("Read"))
        elapsed = time.time() - t0
        out_path = Path(args.output)
        out_path.write_bytes(data)
        print(f"Saved       : {out_path} ({len(data)} bytes in {elapsed:.2f}s)")
        print(f"SUM16       : 0x{sum16(data):08X}")
        print(f"CRC32       : 0x{zlib.crc32(data) & 0xFFFFFFFF:08X}")
        print(f"SHA256      : {hashlib.sha256(data).hexdigest()}")
        return 0
    finally:
        backend.close()


def cmd_verify(args: argparse.Namespace) -> int:
    raw = Path(args.input).read_bytes()
    backend, lp = _open_programmer()
    try:
        chip = lp.find_chip(args.chip, args.vendor)
        params = lp.select_chip(chip)
        expected = pad_image_to_chip(raw, params.size_bytes, args.pad)
        print(f"Verifying   : {chip.vendor_name} {chip.name} ({params.size_bytes} bytes, pad={args.pad})...")
        actual = lp.read_chip(chip, progress_cb=_make_progress_cb("Read"))
        mismatch = verify_buffers(expected, actual)
        if mismatch is None:
            print(f"PASS (100% match, CRC32=0x{zlib.crc32(actual) & 0xFFFFFFFF:08X}, SUM16=0x{sum16(actual):08X})")
            return 0
        off, exp_b, act_b = mismatch
        print(f"FAIL: 最初の不一致アドレス 0x{off:05X} (期待値=0x{exp_b:02X}, 実読値=0x{act_b:02X})")
        return 2
    finally:
        backend.close()


def cmd_program(args: argparse.Namespace) -> int:
    raw = Path(args.input).read_bytes()
    backend, lp = _open_programmer()
    try:
        chip = lp.find_chip(args.chip, args.vendor)
        params = lp.select_chip(chip)
        image = pad_image_to_chip(raw, params.size_bytes, args.pad)

        print(f"Target IC   : {chip.vendor_name} {chip.name} ({params.size_bytes // 1024} KB, ~Vcc {params.vcc_raw/10:.1f}V / ~Vpp {params.vpp_raw/10:.1f}V)")
        print(f"Source File : {args.input} ({len(raw)} bytes -> padded={len(image)} bytes, mode={args.pad})")
        print(f"Image CRC32 : 0x{zlib.crc32(image) & 0xFFFFFFFF:08X}  SUM16=0x{sum16(image):08X}")
        print(f"Image SHA256: {hashlib.sha256(image).hexdigest()}")

        if args.dry_run:
            print("[DRY-RUN] 実機への Erase / Program コマンドは送信しませんでした。")
            return 0

        if not args.yes:
            ans = input("右側の Slave Textool にICを正しくセットしましたか？ 書き込みを開始しますか? [y/N]: ").strip().lower()
            if ans not in ("y", "yes"):
                print("キャンセルしました。")
                return 1

        if args.erase:
            print("Step 1: Erasing Flash/EEPROM...")
            lp.erase(chip)
            print("  Erase PASS")

        if not args.no_check:
            print("Step 2: Blank Check...")
            lp.blank_check(chip)
            print("  Blank Check PASS")

        print("Step 3: Programming...")
        t0 = time.time()
        lp.program_chip(chip, image, progress_cb=_make_progress_cb("Prog"))
        print(f"  Program PASS ({time.time() - t0:.2f}s)")

        if not args.no_verify:
            print("Step 4: Verifying read-back...")
            actual = lp.read_chip(chip, progress_cb=_make_progress_cb("Verify"))
            mismatch = verify_buffers(image, actual)
            if mismatch is not None:
                off, exp_b, act_b = mismatch
                raise LeaperError(
                    f"Verify FAIL at 0x{off:05X}: expected=0x{exp_b:02X}, actual=0x{act_b:02X}"
                )
            print(f"  Verify PASS (100% match! CRC32=0x{zlib.crc32(actual) & 0xFFFFFFFF:08X})")

        return 0
    finally:
        backend.close()


def cmd_split_nes(args: argparse.Namespace) -> int:
    nes_path = Path(args.nes)
    out_dir = Path(args.out_dir) if args.out_dir else nes_path.parent
    res = split_ines_rom(nes_path, out_dir, prg_pad_kb=args.prg_pad_kb, chr_pad_kb=args.chr_pad_kb)
    print(f"ROM     : {nes_path} (Mapper {res['mapper']})")
    print(f"PRG     : {res['prg_size']} bytes -> {res['prg_file']} (CRC32=0x{res['prg_crc32']:08X})")
    if res["prg_padded_file"]:
        print(f"PRG Pad : {res['prg_padded_file']}")
    if res["chr_file"]:
        print(f"CHR     : {res['chr_size']} bytes -> {res['chr_file']} (CRC32=0x{res['chr_crc32']:08X})")
        if res["chr_padded_file"]:
            print(f"CHR Pad : {res['chr_padded_file']}")
    else:
        print("CHR     : 0 bytes (CHR-RAM)")
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    import leaper3c_ui

    return leaper3c_ui.serve_ui(port=args.port, open_browser=not args.no_browser)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="leaper3c",
        description="LEAPER-3C (LP-3C) Flash / EPROM Writer macOS USB Driver & CLI",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("info", help="本体のファームウェア・接続状態・ステータスを表示")
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("list", aliases=["list-chips"], help="本体内蔵の対応IC一覧を表示/検索")
    sp.add_argument("query", nargs="?", help="型番またはメーカーの検索文字 (例: 27C256, 39SF010)")
    sp.add_argument("--vendor", help="メーカー名で絞り込み (例: 27XXX, SST, AMD, WINBOND)")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("chip-info", help="指定ICの容量・電圧パラメータ(0x4B)を表示")
    sp.add_argument("chip", help="IC型番 (例: 27C256, 27C64, 39SF010A)")
    sp.add_argument("--vendor", help="メーカー名")
    sp.set_defaults(func=cmd_chip_info)

    sp = sub.add_parser("blank-check", help="ブランクチェックを実行 (非破壊)")
    sp.add_argument("chip")
    sp.add_argument("--vendor")
    sp.set_defaults(func=cmd_blank_check)

    sp = sub.add_parser("erase", help="Flash/EEPROMを電気消去 (UV EPROM 27C系は不可)")
    sp.add_argument("chip")
    sp.add_argument("--vendor")
    sp.set_defaults(func=cmd_erase)

    sp = sub.add_parser("read", help="ICの内容を読み出してバイナリ保存")
    sp.add_argument("chip")
    sp.add_argument("-o", "--output", required=True, help="出力ファイルパス")
    sp.add_argument("--vendor")
    sp.set_defaults(func=cmd_read)

    sp = sub.add_parser("verify", help="ICの内容を読み出して入力ファイルと照合")
    sp.add_argument("chip")
    sp.add_argument("-i", "--input", required=True, help="比較対象の .bin ファイル")
    sp.add_argument("--vendor")
    sp.add_argument("--pad", choices=("none", "ff", "mirror"), default="none", help="小容量ファイルの拡張方法")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("program", help="ICへ書き込み (Blank Check -> Program -> Verify)")
    sp.add_argument("chip")
    sp.add_argument("-i", "--input", required=True, help="書き込む .bin ファイル")
    sp.add_argument("--vendor")
    sp.add_argument("--pad", choices=("none", "ff", "mirror"), default="none", help="小容量ファイルの拡張方法")
    sp.add_argument("--erase", action="store_true", help="書き込み前にFlash消去を実行")
    sp.add_argument("--no-check", action="store_true", help="事前Blank Checkをスキップ")
    sp.add_argument("--no-verify", action="store_true", help="書き込み後の読出しVerifyをスキップ")
    sp.add_argument("--dry-run", action="store_true", help="実機へ書き込まず事前確認のみ実行")
    sp.add_argument("-y", "--yes", action="store_true", help="確認プロンプトをスキップ")
    sp.set_defaults(func=cmd_program)

    sp = sub.add_parser("split-nes", help=".nes ROM から PRG / CHR バイナリを抽出")
    sp.add_argument("nes", help="入力 .nes ファイル")
    sp.add_argument("-o", "--out-dir", help="出力先ディレクトリ (省略時は .nes と同じ場所)")
    sp.add_argument("--prg-pad-kb", type=int, help="PRGを指定KBへミラー拡張して別ファイル出力 (例: 64, 128)")
    sp.add_argument("--chr-pad-kb", type=int, help="CHRを指定KBへミラー拡張して別ファイル出力 (例: 32, 64, 128)")
    sp.set_defaults(func=cmd_split_nes)

    sp = sub.add_parser("ui", help="ローカルブラウザUIを起動 (.nes分割・挿し方図・HEXプレビュー付き)")
    sp.add_argument("--port", type=int, default=8765, help="待受ポート (デフォルト: 8765)")
    sp.add_argument("--no-browser", action="store_true", help="起動時にブラウザを自動で開かない")
    sp.set_defaults(func=cmd_ui)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except LeaperError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

