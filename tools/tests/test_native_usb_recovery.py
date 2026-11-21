#!/usr/bin/env python3
"""Verify experimental USB restoration on fake transport and the real NOR provisioner."""
from contextlib import nullcontext
from pathlib import Path
import json
import struct
import subprocess
import tempfile
from unittest.mock import patch
import zlib

from hostlib import compiler
from test_native_stage import ROOT, fixture
from build_native_image import LIMIT, SEGMENT
from native_recovery import capture, digest, recover_usb, reconstruct, unpack
from native_snapshot import compile_snapshot, plan_change
from native_update import row_name
from test_native_personal_snapshot import opened, publish_image


class Console:
    serial = "test-spare"

    def __init__(self, data, rows):
        self.data, self.rows = bytearray(data), rows
        self.layout = "Mac U.S."
        self.reset_count = self.begin_count = self.writes = 0
        self.staged_crc = self.final_crc = None
        self.ready = False
        self.supported = True
        self.interrupt = None
        self.erasures = set()
        self.status_override = None
        self.repack_guard = None

    def command(self, line, timeout=30):
        if line == "native_install update recovery-capabilities":
            return "OK RECOVERY1" if self.supported else "ERR Unknown"
        if line == "native_install update repack-capabilities":
            return "OK REPACK1"
        if line.startswith("native_install begin REPACK "):
            _, _, _, staged, final, *guard = line.split()
            if guard != self.repack_guard:
                return "ERR Repack source changed or unavailable; no erase started"
            line = f"native_install begin RECOVERY {staged} {final}"
        if line == "native_install status":
            return self.status_override or ("NATIVE2 READY 2" if self.ready else "NATIVE2 UNPROVISIONED 0")
        if line.startswith("native_install match RECOVERY "):
            return "OK MATCH" if self.ready and zlib.crc32(self.data) == int(line.split()[-1]) else "ERR Recovery contents differ"
        if line.startswith("native_install begin RECOVERY "):
            if self.interrupt == "before-erase":
                raise OSError("Disconnected before erase")
            self.staged_crc, self.final_crc = map(int, line.split()[-2:])
            self.begin_count += 1
            self.erasures.clear()
            self.ready = False
            return "OK"
        if line.startswith("native_install erase "):
            at = int(line.split()[-1])
            self.erasures.add(at)
            self.data[at * SEGMENT:(at + 1) * SEGMENT] = b"\xff" * SEGMENT
            if self.interrupt == "erase" and at == 5:
                self.interrupt = None
                raise OSError("Disconnected while erasing")
            return "OK"
        if line.startswith("native_install write "):
            assert len(self.erasures) == 255
            _, _, address, blob = line.split()
            address = int(address)
            blob = unpack(blob)
            if address in (0, SEGMENT):
                assert blob[28:32] == b"\xff" * 4
            self.data[address:address + len(blob)] = blob
            self.writes += 1
            if self.interrupt == "write":
                self.interrupt = None
                raise OSError("Disconnected while programming")
            return "OK"
        if line == "native_install abort":
            return "OK"
        if line == "native_install finish":
            assert zlib.crc32(self.data) == self.staged_crc
            if self.interrupt == "verify":
                self.interrupt = None
                raise OSError("Disconnected while verifying")
            for i, root in enumerate((0, SEGMENT)):
                if self.interrupt == f"root-{i + 1}":
                    self.data[root + 28:root + 30] = b"TM"
                    self.ready = i == 1
                    raise OSError("Torn publication")
                self.data[root + 28:root + 32] = b"TMOC"
            assert zlib.crc32(self.data) == self.final_crc
            self.ready = True
            if self.interrupt in ("ack", "ack-mismatch"):
                if self.interrupt == "ack-mismatch":
                    self.data[2 * SEGMENT] ^= 1
                self.interrupt = None
                raise OSError("Lost publication reply")
            if self.interrupt == "acknowledged-mismatch":
                self.data[2 * SEGMENT] ^= 1
            return "OK Rebooting"
        if line == "native_catalog":
            assert self.ready
            return json.dumps(self.rows)
        if line.startswith("set_host_layout "):
            self.layout = line.removeprefix("set_host_layout ")
            return "OK"
        if line == "get_parameter host_layout":
            return self.layout
        raise AssertionError(line)

    def wait(self, serial):
        assert serial == self.serial
        self.reset_count += 1
        if not self.ready or self.interrupt == "reconnect":
            raise RuntimeError("not ready")


def main():
    with tempfile.TemporaryDirectory() as folder:
        directory = Path(folder)
        image, original, protected = fixture(directory)
        compact = compile_snapshot("user_dictionary", {(8,): b"recover me", (4, 8): "été".encode()})
        update = plan_change(original, protected, original.personal, compact, convert=True)
        image = publish_image(image, update, 3)
        view = opened(image)
        bundle = capture(lambda start, size: image[start:start + size], view, "Windows U.S. (plain text)")
        expected, _ = reconstruct(bundle)
        rows = [dict(id=struct.unpack_from("<I", row)[0], name=row_name(row), enabled=bool(row[4]))
                for row in view.rows]

        def attempt(usb, confirmation=Console.serial):
            with patch("native_install.open_console", return_value=nullcontext(usb)),                  patch("native_install.wait_ready", usb.wait):
                recover_usb(bundle, usb.serial, confirmation, lambda _: None)

        for interruption in (None, "erase", "write", "verify", "ack"):
            usb = Console(b"\xff" * LIMIT if interruption is None else b"\xab" * LIMIT, rows)
            usb.interrupt = interruption
            if interruption in ("erase", "write", "verify"):
                try:
                    attempt(usb)
                except (OSError, RuntimeError):
                    pass
                else:
                    raise AssertionError("Interrupted recovery reported success")
                assert not usb.ready and usb.begin_count == 1
            attempt(usb)
            before = usb.begin_count
            assert usb.data == expected and usb.layout == bundle["host_layout"]
            attempt(usb)
            assert usb.begin_count == before

        for interruption, erased in (("before-erase", False), ("root-1", True), ("root-2", True),
                                     ("acknowledged-mismatch", True), ("reconnect", True)):
            usb = Console(b"\xff" * LIMIT, rows)
            usb.interrupt = interruption
            try:
                attempt(usb)
            except (OSError, RuntimeError):
                pass
            else:
                raise AssertionError(f"Interrupted recovery claimed success at {interruption}")
            assert bool(usb.erasures) == erased and usb.begin_count == int(erased)
            if interruption == "reconnect":
                usb.interrupt = None
                attempt(usb)
                assert usb.begin_count == 1 and usb.data == expected

        optimized = dict(bundle, repack={})
        optimized["sha256"] = digest(optimized)
        spare = Console(b"\xff" * LIMIT, rows)
        with patch("native_install.open_console", return_value=nullcontext(spare)), patch("native_install.wait_ready", spare.wait):
            try:
                recover_usb(optimized, spare.serial, spare.serial, lambda _: None)
            except ValueError:
                pass
            else:
                raise AssertionError("Generic recovery accepted an optimized bundle")
            for status in ("NATIVE2 READY 65", "NATIVE2 INSTALL 65"):
                spare.status_override = status
                try:
                    recover_usb(optimized, spare.serial, spare.serial, lambda _: None, repack_spare=True)
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("Optimized recovery tried to replace a live or busy pool")
                assert not spare.erasures
            spare.status_override = None
            recover_usb(optimized, spare.serial, spare.serial, lambda _: None, repack_spare=True)
            recover_usb(optimized, spare.serial, spare.serial, lambda _: None, repack_spare=True)
            assert spare.begin_count == 1 and spare.data == expected

        live = Console(b"\xab" * LIMIT, rows)
        live.ready = True
        source = dict(bundle, capture=dict(serial=live.serial, generation=7, timestamp="2026-11-20T01:00:00Z"))
        view = reconstruct(bundle)[1]
        guard = ["7", "0", str(zlib.crc32(view.payload)), str(view.highwater), "0", "1"]
        live.repack_guard = guard.copy()
        live.repack_guard[3] = str(view.highwater + 1)
        with patch("native_install.open_console", return_value=nullcontext(live)), patch("native_install.wait_ready", live.wait), patch("native_repack.check_live", return_value={}):
            try:
                recover_usb(optimized, live.serial, live.serial, lambda _: None, live_source=(source, view))
            except RuntimeError as error:
                assert "source changed" in str(error)
            else:
                raise AssertionError("A journal edit between host validation and guarded begin was accepted")
            assert not live.erasures and live.begin_count == 0
            live.repack_guard = guard
            recover_usb(optimized, live.serial, live.serial, lambda _: None, live_source=(source, view))
            assert live.data == expected and live.begin_count == 1

        usb = Console(expected, rows)
        usb.ready = True
        with patch("native_install.open_console", return_value=nullcontext(usb)):
            try:
                recover_usb(bundle, usb.serial, usb.serial, lambda _: None, unprovisioned_only=True)
            except RuntimeError as error:
                assert "already has a readable dictionary pool" in str(error)
            else:
                raise AssertionError("Manager backup recovery was allowed on a readable pool")
            assert not usb.erasures and usb.begin_count == 0

        usb = Console(b"\xff" * LIMIT, rows)
        for supported, serial in ((True, "wrong-device"), (False, Console.serial)):
            usb.supported = supported
            try:
                attempt(usb, serial)
            except (ValueError, RuntimeError):
                pass
            else:
                raise AssertionError("Unconfirmed or unsupported recovery proceeded")
            assert usb.begin_count == 0
        wrong_device = Console(b"\xff" * LIMIT, rows)
        wrong_device.serial = "different-physical-device"
        with patch("native_install.open_console", return_value=nullcontext(wrong_device)):
            try:
                recover_usb(bundle, Console.serial, Console.serial)
            except RuntimeError as exc:
                assert "differs" in str(exc)
            else:
                raise AssertionError("Wrong observed HID serial reached recovery")
            assert not wrong_device.begin_count and not wrong_device.erasures

        uncertain = Console(b"\xff" * LIMIT, rows)
        uncertain.interrupt = "ack-mismatch"
        try:
            attempt(uncertain)
        except RuntimeError as exc:
            assert "Publication could not be confirmed" in str(exc)
        else:
            raise AssertionError("Mismatched pool CRC accepted after an uncertain acknowledgement")
        assert uncertain.begin_count == 1 and uncertain.layout == "Mac U.S."

        with patch("native_install.open_console") as connect:
            corrupt = dict(bundle, sha256="corrupt")
            try:
                recover_usb(corrupt, Console.serial, Console.serial)
            except ValueError:
                pass
            else:
                raise AssertionError("Corrupt bundle accepted")
            connect.assert_not_called()

        binary = directory / "recover"
        subprocess.run([*compiler(), "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-I", str(ROOT / "tools/storage"), str(ROOT / "tools/tests/test_native_usb_recovery.cc"),
                        "-o", str(binary)], check=True)
        subprocess.run([str(binary)], check=True)
        print("USB workflow verified blank/partial recovery, exact output, settings, retry, lost acknowledgments and erase gating")


if __name__ == "__main__":
    main()
