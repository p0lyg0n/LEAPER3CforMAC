#!/usr/bin/env python3
"""
minipro-compatible CLI shim for LEAPER-3C (LP-3C FLASH WRITER).

Allows open-source minipro frontends such as FireMinipro (Qt6) to detect,
query, read, blank-check, erase, program, and verify chips connected to the
LEAPER-3C over libusb/PyUSB without modifying the GUI application.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

import leaper3c  # noqa: E402


PROGRAMMER_ID = "LEAPER-3C"


def resolve_device_spec(lp: leaper3c.Leaper3C, spec: str) -> leaper3c.ChipEntry:
    """Resolve 'CHIP@VENDOR' or plain 'CHIP' against LP-3C's 260-IC table."""
    raw = spec.strip()
    if "@" in raw:
        chip_part, vendor_part = raw.split("@", 1)
        return lp.find_chip(chip_part.strip(), vendor_part.strip() or None)
    return lp.find_chip(raw)


def format_device_name(chip: leaper3c.ChipEntry) -> str:
    return f"{chip.name}@{chip.vendor_name}"


def _is_leaper_connected() -> bool:
    try:
        backend = leaper3c.PyUsbBackend()
        backend.close()
        return True
    except leaper3c.LeaperError:
        return False


def _fallback_orig_minipro(argv: list[str]) -> Optional[int]:
    orig = os.environ.get("MINIPRO_ORIG_BIN") or "/Applications/fireminipro.app/Contents/MacOS/minipro.orig"
    if orig and Path(orig).is_file() and os.access(orig, os.X_OK):
        res = subprocess.run([orig, *argv], check=False)
        return res.returncode
    return None


def _open_lp() -> tuple[leaper3c.PyUsbBackend, leaper3c.Leaper3C]:
    backend = leaper3c.PyUsbBackend()
    return backend, leaper3c.Leaper3C(backend)


def cmd_scan(argv: list[str]) -> int:
    found_any = False
    try:
        backend, lp = _open_lp()
        try:
            info = lp.get_info()
            print(f"Programmer 0: {PROGRAMMER_ID}; FW {info.fw_version}, USB ({backend.usb_location})")
            found_any = True
        finally:
            backend.close()
    except leaper3c.LeaperError:
        pass

    orig = os.environ.get("MINIPRO_ORIG_BIN") or "/Applications/fireminipro.app/Contents/MacOS/minipro.orig"
    if orig and Path(orig).is_file() and os.access(orig, os.X_OK):
        proc = subprocess.run([orig, *argv], capture_output=True, text=True, check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            for line in proc.stdout.splitlines():
                if "no programmer found" not in line.lower():
                    print(line)
                    found_any = True

    if not found_any:
        print("No programmer found.", file=sys.stderr)
        return 1
    return 0


def cmd_list(lp: leaper3c.Leaper3C, pattern: Optional[str]) -> int:
    chips = lp.list_chips()
    pat = pattern.strip().upper() if pattern else None
    for c in chips:
        label = format_device_name(c)
        if pat and pat not in label.upper():
            continue
        print(label)
    return 0


def cmd_chip_info(lp: leaper3c.Leaper3C, spec: str) -> int:
    chip = resolve_device_spec(lp, spec)
    params = lp.select_chip(chip)
    print(f"Name: {format_device_name(chip)}")
    print(f"Memory: {params.size_bytes} Bytes")
    print("Protocol: 0x49")
    print(f"Read buffer size: {leaper3c.BLOCK_SIZE} Bytes")
    print(f"Write buffer size: {leaper3c.BLOCK_SIZE} Bytes")
    print(f"Vcc: {params.vcc_raw / 10:.1f}V")
    if chip.is_uv_eprom:
        print(f"Vpp: {params.vpp_raw / 10:.1f}V (UV-EPROM)")
    return 0


def _pct_printer(phase: str) -> callable:
    last_pct = [-1]

    def cb(done: int, total: int) -> None:
        pct = int(done * 100 / total) if total else 100
        if pct != last_pct[0]:
            last_pct[0] = pct
            print(f"{phase} {pct}%", flush=True)

    return cb


def cmd_read(lp: leaper3c.Leaper3C, spec: str, out_path: str) -> int:
    chip = resolve_device_spec(lp, spec)
    data = lp.read_chip(chip, progress_cb=_pct_printer("Reading Code..."))
    Path(out_path).write_bytes(data)
    print(f"Success: Read {len(data)} bytes to {out_path}", flush=True)
    return 0


def cmd_blank(lp: leaper3c.Leaper3C, spec: str) -> int:
    chip = resolve_device_spec(lp, spec)
    lp.blank_check(chip)
    print(f"{format_device_name(chip)} is blank! Success.", flush=True)
    return 0


def cmd_erase(lp: leaper3c.Leaper3C, spec: str) -> int:
    chip = resolve_device_spec(lp, spec)
    lp.erase(chip)
    print(f"Erase OK (Success)", flush=True)
    return 0


def cmd_verify(lp: leaper3c.Leaper3C, spec: str, in_path: str, ignore_size: bool) -> int:
    chip = resolve_device_spec(lp, spec)
    params = lp.select_chip(chip)
    raw = Path(in_path).read_bytes()
    pad_mode = "ff" if ignore_size and len(raw) < params.size_bytes else "none"
    expected = leaper3c.pad_image_to_chip(raw, params.size_bytes, pad_mode)
    actual = lp.read_chip(chip, progress_cb=_pct_printer("Reading Code..."))
    mismatch = leaper3c.verify_buffers(expected, actual)
    if mismatch is not None:
        off, exp_b, act_b = mismatch
        print(
            f"Error: Verification failed at address 0x{off:05X}: file=0x{exp_b:02X}, device=0x{act_b:02X}",
            file=sys.stderr,
        )
        return 1
    print("Verification OK", flush=True)
    return 0


def cmd_write(
    lp: leaper3c.Leaper3C,
    spec: str,
    in_path: str,
    no_erase: bool,
    skip_verify: bool,
    ignore_size: bool,
) -> int:
    chip = resolve_device_spec(lp, spec)
    params = lp.select_chip(chip)
    raw = Path(in_path).read_bytes()
    if len(raw) > params.size_bytes:
        if not ignore_size:
            raise leaper3c.LeaperError(
                f"Incorrect file size: {len(raw)} bytes > IC capacity {params.size_bytes} bytes"
            )
        raw = raw[: params.size_bytes]
    pad_mode = "ff" if len(raw) < params.size_bytes else "none"
    image = leaper3c.pad_image_to_chip(raw, params.size_bytes, pad_mode)

    if not no_erase and not chip.is_uv_eprom:
        lp.erase(chip)
        print("Erase OK", flush=True)

    lp.blank_check(chip)
    print("Chip is blank", flush=True)

    lp.program_chip(chip, image, progress_cb=_pct_printer("Writing Code..."))
    print("Writing Code... 100% Success", flush=True)

    if not skip_verify:
        actual = lp.read_chip(chip, progress_cb=_pct_printer("Reading Code..."))
        mismatch = leaper3c.verify_buffers(image, actual)
        if mismatch is not None:
            off, exp_b, act_b = mismatch
            raise leaper3c.LeaperError(
                f"Verification failed at 0x{off:05X}: expected=0x{exp_b:02X}, actual=0x{act_b:02X}"
            )
        print("Verification OK", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="minipro", add_help=True)
    p.add_argument("-k", "--scan", action="store_true", help="Scan connected programmers")
    p.add_argument("-q", "--programmer", help="Force programmer name")
    p.add_argument("-l", "--list", nargs="?", const="", default=None, help="List supported devices")
    p.add_argument("-L", "--list-alt", nargs="?", const="", default=None, help="List supported devices")
    p.add_argument("-d", "--device-info", help="Print device info")
    p.add_argument("-p", "--device", help="Target IC device")
    p.add_argument("-r", "--read", help="Read IC to file")
    p.add_argument("-w", "--write", help="Write file to IC")
    p.add_argument("-m", "--verify", help="Verify IC against file")
    p.add_argument("-b", "--blank", action="store_true", help="Blank check")
    p.add_argument("-E", "--erase", action="store_true", help="Erase IC")
    p.add_argument("-e", "--no-erase", action="store_true", help="Do not erase before write")
    p.add_argument("-v", "--skip-verify", action="store_true", help="Do not verify after write")
    p.add_argument("-s", "--no-size-error", action="store_true", help="Ignore file size mismatch")
    p.add_argument("-S", "--no-size-warning", action="store_true", help="Ignore size warning")
    p.add_argument("-y", "--yes", action="store_true", help="Ignore ID error / confirm")
    p.add_argument("-x", "--skip-id", action="store_true", help="Skip ID check")
    p.add_argument("-c", "--mem-type", help="Memory type (code/data)")
    p.add_argument("-V", "--version", action="store_true", help="Show version")
    p.add_argument("-T", "--logic-test", action="store_true", help="Logic IC test (unsupported on LP-3C)")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args, _unknown = parser.parse_known_args(raw_argv)

    if args.version:
        print("minipro-leaper3c shim v1.0 (compatible with LEAPER-3C FW 1.30)")
        return 0

    if args.scan:
        return cmd_scan(raw_argv)

    # If user explicitly chose another programmer (e.g. T48/TL866) or LP-3C is absent,
    # fall back to minipro.orig if present.
    if args.programmer and PROGRAMMER_ID not in args.programmer.upper():
        rc = _fallback_orig_minipro(raw_argv)
        if rc is not None:
            return rc

    if not _is_leaper_connected():
        rc = _fallback_orig_minipro(raw_argv)
        if rc is not None:
            return rc

    try:
        backend, lp = _open_lp()
    except leaper3c.LeaperError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        if args.list is not None or args.list_alt is not None:
            pat = args.list if args.list is not None else args.list_alt
            return cmd_list(lp, pat)
        if args.device_info:
            return cmd_chip_info(lp, args.device_info)
        if args.logic_test:
            print("Error: Logic IC test (-T) is not supported on LEAPER-3C.", file=sys.stderr)
            return 1
        if not args.device:
            print("Error: No device specified (-p <device>).", file=sys.stderr)
            return 1

        ignore_size = bool(args.no_size_error or args.no_size_warning)
        if args.read:
            return cmd_read(lp, args.device, args.read)
        if args.write:
            return cmd_write(lp, args.device, args.write, args.no_erase, args.skip_verify, ignore_size)
        if args.verify:
            return cmd_verify(lp, args.device, args.verify, ignore_size)
        if args.blank:
            return cmd_blank(lp, args.device)
        if args.erase:
            return cmd_erase(lp, args.device)

        print("Error: No action specified (-r, -w, -m, -b, -E, -l, -d, -k).", file=sys.stderr)
        return 1
    except leaper3c.LeaperError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        backend.close()


if __name__ == "__main__":
    raise SystemExit(main())
