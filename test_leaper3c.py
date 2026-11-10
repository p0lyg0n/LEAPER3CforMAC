#!/usr/bin/env python3
"""Unit & Hardware-Protocol Tests for leaper3c.py."""

import tempfile
import unittest
from pathlib import Path

import leaper3c


class FakeUsbBackend:
    """In-memory LP-3C USB simulator capturing exact Setup/Bulk sequences."""

    def __init__(self, fail_status_0: int = 0, fail_status_1: int = 0) -> None:
        self.calls: list[tuple] = []
        self.ram = bytearray(65536)
        self.chip_mem = bytearray(b"\xff" * 2048)
        self.fail_status_0 = fail_status_0
        self.fail_status_1 = fail_status_1
        self.block_ptr = 0
        self.selected_vendor = 0
        self.selected_dev = 0

    def ctrl_read(self, req: int, val: int, idx: int, length: int, timeout: int = 5000) -> bytes:
        self.calls.append(("ctrl_read", req, val, idx, length))
        if req == 0x41:
            return b"  FLASH WRITER  1.30\x1e\xd6"
        if req == 0x42:
            return bytes([self.fail_status_0, self.fail_status_1, 0, 0x20, 1, 0, 0xFF, 0])
        if req == 0x44:
            return bytes(self.ram[val : val + length])
        if req == 0x4B:
            self.selected_vendor = val
            self.selected_dev = idx
            # Simulate 1024B test size: byte30=4, byte31=0 => lo=4? No: 0x04 << 8 = 1024 => b31=4, b30=0
            resp = bytearray(34)
            resp[18] = 0x33
            resp[20] = 0x7F
            resp[30] = 0
            resp[31] = 4  # 4 << 8 = 1024 bytes (2 blocks)
            return bytes(resp)
        raise AssertionError(f"Unexpected ctrl_read req=0x{req:02X}")

    def ctrl_write(self, req: int, val: int, idx: int, data: bytes = b"", timeout: int = 5000) -> int:
        self.calls.append(("ctrl_write", req, val, idx, data))
        if req == 0x49 and idx in (leaper3c.CMD_SESSION_START, leaper3c.CMD_PROG_START):
            self.block_ptr = 0
        elif req == 0x49 and idx == leaper3c.CMD_READ_BLOCK:
            start = self.block_ptr * 512
            self.ram[0x800 : 0xA00] = self.chip_mem[start : start + 512]
            self.block_ptr += 1
        elif req == 0x49 and idx == leaper3c.CMD_PROG_BLOCK:
            start = self.block_ptr * 512
            self.chip_mem[start : start + 512] = self.ram[0x800 : 0xA00]
            self.block_ptr += 1
        elif req == 0x47:
            self._next_offset = val
        return len(data)

    def bulk_write(self, data: bytes, timeout: int = 5000) -> int:
        self.calls.append(("bulk_write", len(data), bytes(data[:4])))
        self.ram[self._next_offset : self._next_offset + len(data)] = data
        return len(data)


class TestLeaper3C(unittest.TestCase):
    def test_decode_chip_size_all_variants(self) -> None:
        # 27C64: 8KB
        p64 = bytes.fromhex("0e 00 02 00 02 00 00 00 00 00 12 00 1e 00 1e 00 1e 00 33 00 81 00 08 00 64 00 0a 00 00 00 00 20 00 00")
        self.assertEqual(leaper3c.decode_chip_size(p64), 8192)
        # 27C256: 32KB
        p256 = bytes.fromhex("0c 00 02 00 02 00 00 00 00 00 0e 00 1e 00 1e 00 1e 00 33 00 7f 00 08 00 64 00 0a 00 00 00 00 80 00 00")
        self.assertEqual(leaper3c.decode_chip_size(p256), 32768)
        # 27C512: 64KB
        p512 = bytes.fromhex("24 00 02 00 08 00 00 00 00 00 10 00 1e 00 1e 00 1e 00 33 00 81 00 08 00 64 00 0a 00 01 00 00 00 00 00")
        self.assertEqual(leaper3c.decode_chip_size(p512), 65536)
        # SST39SF010A: 128KB
        p10 = bytes.fromhex("14 00 04 00 00 00 00 00 00 00 00 00 00 00 1e 00 1e 00 33 00 7a 00 08 00 0f 00 0a 00 02 00 00 00 00 00")
        self.assertEqual(leaper3c.decode_chip_size(p10), 131072)

    def test_pad_image_to_chip(self) -> None:
        chr_data = bytes(range(256)) * 32  # 8192 bytes
        mirrored = leaper3c.pad_image_to_chip(chr_data, 32768, "mirror")
        self.assertEqual(len(mirrored), 32768)
        self.assertEqual(mirrored[8192:16384], chr_data)

        ff_padded = leaper3c.pad_image_to_chip(chr_data, 32768, "ff")
        self.assertEqual(ff_padded[:8192], chr_data)
        self.assertEqual(set(ff_padded[8192:]), {0xFF})

        with self.assertRaises(leaper3c.LeaperError):
            leaper3c.pad_image_to_chip(chr_data, 32768, "none")

    def test_program_and_read_roundtrip_mock(self) -> None:
        fake = FakeUsbBackend()
        lp = leaper3c.Leaper3C(fake)
        chip = leaper3c.ChipEntry(vendor_id=16, vendor_name="SST", dev_id=2, name="39SF010A", prefix_idx=11, flags=1)

        lp.blank_check(chip)
        # Verify vendor_dummy sent 16 bytes for vendor_id=16
        bc_cmd = [c for c in fake.calls if c[0] == "ctrl_write" and c[1] == 0x49 and c[3] == 0x59][0]
        self.assertEqual(bc_cmd[2], 2)
        self.assertEqual(len(bc_cmd[4]), 16)

        payload = bytes((i * 7 + 3) & 0xFF for i in range(1024))
        lp.program_chip(chip, payload)
        readback = lp.read_chip(chip)
        self.assertEqual(readback, payload)
        self.assertIsNone(leaper3c.verify_buffers(payload, readback))

        # Verify over-current status throws
        fake_oc = FakeUsbBackend(fail_status_0=1, fail_status_1=8)
        lp_oc = leaper3c.Leaper3C(fake_oc)
        with self.assertRaises(leaper3c.LeaperError) as ctx:
            lp_oc.blank_check(chip)
        self.assertIn("OVER CURRENT", str(ctx.exception))

    def test_uv_eprom_erase_rejected(self) -> None:
        fake = FakeUsbBackend()
        lp = leaper3c.Leaper3C(fake)
        eprom = leaper3c.ChipEntry(vendor_id=0, vendor_name="27XXX", dev_id=2, name="27C256", prefix_idx=25, flags=3)
        with self.assertRaises(leaper3c.LeaperError):
            lp.erase(eprom)

    def test_split_ines_rom(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            nes = d / "test_nrom.nes"
            prg = b"\xEA" * 32768
            chr_rom = b"\x55" * 8192
            header = bytearray(16)
            header[0:4] = b"NES\x1a"
            header[4] = 2  # 32KB PRG
            header[5] = 1  # 8KB CHR
            nes.write_bytes(bytes(header) + prg + chr_rom)

            res = leaper3c.split_ines_rom(nes, d / "out", prg_pad_kb=64, chr_pad_kb=32)
            self.assertEqual(res["mapper"], 0)
            self.assertEqual(res["prg_size"], 32768)
            self.assertEqual(res["chr_size"], 8192)
            self.assertEqual(Path(res["prg_file"]).read_bytes(), prg)
            self.assertEqual(Path(res["chr_file"]).read_bytes(), chr_rom)
            self.assertEqual(len(Path(res["prg_padded_file"]).read_bytes()), 65536)
            self.assertEqual(len(Path(res["chr_padded_file"]).read_bytes()), 32768)

    def test_minipro_shim_fireminipro_contract(self) -> None:
        import io
        from contextlib import redirect_stdout
        import minipro_shim

        fake = FakeUsbBackend()
        lp = leaper3c.Leaper3C(fake)
        # Seed minimal chip table in fake RAM at 0xD61E so parse_chip_table works if list_chips called
        chips = [
            leaper3c.ChipEntry(vendor_id=0, vendor_name="27XXX", dev_id=2, name="27C256", prefix_idx=25, flags=3),
            leaper3c.ChipEntry(vendor_id=16, vendor_name="SST", dev_id=2, name="39SF010A", prefix_idx=11, flags=1),
        ]
        lp._chips_cache = chips

        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(minipro_shim.cmd_list(lp, "27C"), 0)
        self.assertEqual(buf.getvalue().strip(), "27C256@27XXX")

        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(minipro_shim.cmd_chip_info(lp, "27C256@27XXX"), 0)
        out = buf.getvalue()
        self.assertIn("Name: 27C256@27XXX", out)
        self.assertIn("Memory: 1024 Bytes", out)
        self.assertIn("Read buffer size: 512 Bytes", out)

        with tempfile.TemporaryDirectory() as td:
            in_bin = Path(td) / "in.bin"
            out_bin = Path(td) / "out.bin"
            payload = bytes((i * 11) & 0xFF for i in range(1024))
            in_bin.write_bytes(payload)

            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(
                    minipro_shim.cmd_write(lp, "27C256@27XXX", str(in_bin), no_erase=False, skip_verify=False, ignore_size=False),
                    0,
                )
            self.assertIn("Verification OK", buf.getvalue())

            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(minipro_shim.cmd_read(lp, "27C256@27XXX", str(out_bin)), 0)
            self.assertEqual(out_bin.read_bytes(), payload)

    def test_web_ui_http_endpoints(self) -> None:
        import base64
        import json
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        import leaper3c_ui

        fake = FakeUsbBackend()

        def factory():
            return fake

        state = leaper3c_ui.UiState(backend_factory=factory)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), leaper3c_ui.make_handler(state))
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            port = srv.server_address[1]
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as r:
                html = r.read().decode("utf-8")
                self.assertIn("LEAPER-3C ROMライター UI", html)

            # Inspect .nes upload via POST /api/inspect-file
            header = bytearray(16)
            header[0:4] = b"NES\x1a"
            header[4] = 2
            header[5] = 1
            rom = bytes(header) + (b"\xAA" * 32768) + (b"\x55" * 8192)
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/inspect-file",
                data=json.dumps({"filename": "demo.nes", "data_b64": base64.b64encode(rom).decode("ascii")}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req) as r:
                parsed = json.loads(r.read().decode("utf-8"))
                self.assertTrue(parsed["is_nes"])
                self.assertEqual(parsed["nes_format"], "iNES")
                self.assertEqual(parsed["header_bytes"], 16)
                self.assertEqual(parsed["trainer_bytes"], 0)
                self.assertEqual(len(parsed["parts"]), 2)
                self.assertEqual(parsed["parts"][0]["size"], 32768)
                self.assertEqual(parsed["parts"][1]["size"], 8192)

            # Verify log history retains header-stripping entry and supports clearing
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/job") as r:
                job_data = json.loads(r.read().decode("utf-8"))
                self.assertTrue(any("ヘッダ検出 (16 bytes)" in line for line in job_data["logs"]))

            clear_req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/clear-logs",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(clear_req) as r:
                self.assertTrue(json.loads(r.read().decode("utf-8"))["ok"])
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/job") as r:
                self.assertEqual(json.loads(r.read().decode("utf-8"))["logs"], [])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_bundled_libusb_search_and_launcher_helpers(self) -> None:
        import sys
        import tempfile
        from pathlib import Path
        import webui_launcher_entry

        with tempfile.TemporaryDirectory() as tmp:
            orig_meipass = getattr(sys, "_MEIPASS", None)
            sys._MEIPASS = tmp
            try:
                paths = leaper3c._candidate_libusb_paths()
                self.assertEqual(paths[0], Path(tmp) / "libusb-1.0.dylib")
            finally:
                if orig_meipass is None:
                    delattr(sys, "_MEIPASS")
                else:
                    sys._MEIPASS = orig_meipass

            log_file = Path(tmp) / "webui.log"
            log_file.write_text("line1\nline2\nERROR: boom\n", encoding="utf-8")
            self.assertIn("ERROR: boom", webui_launcher_entry.read_log_tail(log_file, 6))

    def test_web_ui_offline_chip_fallback(self) -> None:
        import json
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        import leaper3c_ui

        def fail_factory():
            raise leaper3c.LeaperError("LEAPER-3C not connected")

        state = leaper3c_ui.UiState(backend_factory=fail_factory)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), leaper3c_ui.make_handler(state))
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            port = srv.server_address[1]
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/chips") as r:
                chips_res = json.loads(r.read().decode("utf-8"))
                self.assertEqual(chips_res["source"], "offline")
                self.assertTrue(any(c["name"] == "27C256" and c["vendor"] == "27XXX" for c in chips_res["chips"]))

            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/chip-info",
                data=json.dumps({"name": "27C256", "vendor": "27XXX"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req) as r:
                info = json.loads(r.read().decode("utf-8"))
                self.assertEqual(info["source"], "offline")
                self.assertEqual(info["size_bytes"], 32768)
                self.assertEqual(info["pin_count"], 28)
                self.assertTrue(info["is_uv_eprom"])

            # Verify quick status check skips cleanly if usb_lock is held and detects disconnect
            self.assertTrue(state.usb_lock.acquire(blocking=False))
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status?quick=1") as r:
                    self.assertEqual(json.loads(r.read().decode("utf-8")), {"busy": True})
            finally:
                state.usb_lock.release()
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status?quick=1") as r:
                self.assertFalse(json.loads(r.read().decode("utf-8"))["connected"])

            # Verify English localization via X-Ui-Lang header for disconnected errors
            en_req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/status",
                headers={"X-Ui-Lang": "en"},
            )
            with urllib.request.urlopen(en_req) as r:
                en_st = json.loads(r.read().decode("utf-8"))
                self.assertFalse(en_st["connected"])
            import re
            err_msg = leaper3c_ui._localize_error(
                "LEAPER-3C (VID=0x04CE, PID=0x0A3C) が見つかりません。USBケーブルと12V電源スイッチを確認してください。",
                "en",
            )
            self.assertIsNone(re.search(r"[ぁ-んァ-ン一-龥]", err_msg))
            self.assertIn("not found. Please check the USB cable", err_msg)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()

