#!/usr/bin/env python3
"""Drive the Manager-side binary transfer client against the real firmware logic.

The device is transfer_device_sim.cc: the real NativeStage on simulated NOR behind
the real FirmwarePort, spoken to with 63-byte reports over pipes. After each upload
the simulator compares its flash with the legacy erase/write/commit result.
"""
from hostlib import compiler
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
for sub in ("tools/tests", "tools/storage", "tools/dictionaries", "tools/dict_compiler"):
    sys.path.insert(0, str(ROOT / sub))

import native_transfer as nt
import native_update
from test_native_stage import fixture, plan, write_plan

KIND_COMPACT, KIND_PERSONAL = 0, 1


class SimDevice:
    """HID device double backed by the simulator, with an optionally lossy link."""

    def __init__(self, binary, before, plan_path, kind, *, loss=0.0, corrupt=0.0, duplicate=0.0, reorder=0.0, seed=1):
        self.process = subprocess.Popen([binary, before, plan_path, str(kind)], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, bufsize=0)
        self.rng = random.Random(seed)
        self.loss, self.corrupt, self.duplicate, self.reorder = loss, corrupt, duplicate, reorder
        self.replies = []
        self.writes = 0
        self.dead = False

    def _damage(self, report):
        report = bytearray(report)
        if self.rng.random() < self.corrupt:
            report[self.rng.randrange(len(report))] ^= 1 << self.rng.randrange(8)
        return bytes(report)

    def write(self, data):
        self.writes += 1
        if self.dead:
            return len(data)
        report = bytes(data[1:])
        copies = 2 if self.rng.random() < self.duplicate else 1
        for _ in range(copies):
            if self.rng.random() < self.loss:
                continue
            self.process.stdin.write(b"R" + self._damage(report))
            tag = self.process.stdout.read(1)
            if tag == b"R":
                reply = self.process.stdout.read(63)
                if self.rng.random() >= self.loss:
                    self.replies.append(self._damage(reply))
                    if self.rng.random() < self.duplicate:
                        self.replies.append(self.replies[-1])
        return len(data)

    def read(self, size, timeout):
        if self.replies:
            index = 1 if len(self.replies) > 1 and self.rng.random() < self.reorder else 0
            return list(self.replies.pop(index))
        time.sleep(min(timeout / 1000, 0.0005))
        return []

    def kill(self):
        self.process.kill()
        self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()

    def finish(self):
        self.process.stdin.write(b"E")
        line = self.process.stdout.readline().decode().strip()
        self.process.wait(timeout=30)
        self.process.stdin.close()
        self.process.stdout.close()
        return line


class Console:
    def __init__(self, device, kind, capable=True):
        self.device, self.kind, self.capable, self.commands = device, kind, capable, []

    def command(self, text, timeout=30):
        self.commands.append(text)
        if "transfer-capabilities" in text:
            return "OK XFER1 49" if self.capable else "ERR Unknown native update command"
        if "transfer-kind" in text:
            return f"OK {self.kind}"
        return "OK"


class Fixture:
    @classmethod
    def build(cls):
        cls.folder = tempfile.TemporaryDirectory(prefix="steno-xfer-client-")
        path = Path(cls.folder.name)
        image, snapshot, protected = fixture(path)
        cls.before = path / "before.bin"
        cls.before.write_bytes(image)
        cls.plans = {
            "import": (plan(snapshot, protected, {(1,): b"changed", (7, 8): "ça".encode()}), KIND_PERSONAL),
            "compact": (plan(snapshot, protected), KIND_COMPACT),
        }
        for name, (update, _) in cls.plans.items():
            write_plan(path / f"{name}.bin", update)
        cls.binary = path / "sim"
        subprocess.run([*compiler(), "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror", "-I", str(ROOT / "tools/storage"),
                        ROOT / "tools/tests/transfer_device_sim.cc", "-o", cls.binary], check=True)
        cls.path = path

    def device(self, name, **fault):
        update, kind = self.plans[name]
        return update, kind, SimDevice(self.binary, self.before, self.path / f"{name}.bin", kind, **fault)


class TransferClientTests(Fixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build()

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def test_golden_frame_and_decoder(self):
        golden = bytes.fromhex("b502020110000000030041424332937f59")
        self.assertEqual(nt.encode(nt.DATA, 0x0102, 0x10, b"ABC"), golden)
        decoder = nt.Decoder()
        a = nt.encode(nt.DATA, 7, 0, bytes([0xB5, 0xB5, 1, 2]))
        bad = bytearray(nt.encode(nt.DATA, 7, 4, b"\x09\x09"))
        bad[10] ^= 0x40
        b = nt.encode(nt.END, 7, 4)
        frames = []
        for byte in a + b"\0\0\0" + bytes(bad) + b:
            frames += decoder.push(bytes([byte]))
        self.assertEqual([f[0] for f in frames], [nt.DATA, nt.END])
        self.assertEqual(frames[0][3], bytes([0xB5, 0xB5, 1, 2]))
        self.assertGreaterEqual(decoder.crc_errors, 1)
        with self.assertRaises(ValueError):
            nt.encode(nt.DATA, 0, 0)

    def upload(self, name, **fault):
        update, kind, device = self.device(name, **fault)
        console = Console(device, kind)
        messages = []
        try:
            nt.upload(console, update, messages.append, stall_seconds=0.02 if fault else 1.0, retries=200)
            return device.finish(), device, messages
        except BaseException:
            device.kill()
            raise

    def test_clean_upload_matches_legacy_flash(self):
        for name in ("import", "compact"):
            with self.subTest(name):
                result, device, messages = self.upload(name)
                self.assertRegex(result, r"^MATCH \d+$")
                self.assertEqual(messages[0], "Writing dictionary data… 0%")
                self.assertTrue(any("100%" in m for m in messages))

    def test_lossy_link_still_matches_or_fails_loudly(self):
        for seed in range(1, 9):
            with self.subTest(seed=seed):
                result, device, _ = self.upload("import", loss=0.06, corrupt=0.04, duplicate=0.05, reorder=0.3, seed=seed)
                self.assertRegex(result, r"^MATCH \d+$")

    def test_dead_link_raises_after_data_started_and_never_falls_back(self):
        update, kind, device = self.device("import")
        console = Console(device, kind)
        original = device.write
        sent = []

        def dying(data):
            sent.append(1)
            if len(sent) > 6:
                device.dead = True
            return original(data)
        device.write = dying
        with self.assertRaisesRegex(RuntimeError, "stopped responding"):
            nt.upload(console, update, lambda _: None, stall_seconds=0.01, retries=3)
        device.kill()
        self.assertFalse(any(c.startswith("native_install update write") for c in console.commands))

    def test_publish_uses_binary_when_advertised(self):
        update, kind, device = self.device("compact")
        console = Console(device, kind)
        native_update.publish(console, update, lambda _: None)
        self.assertRegex(device.finish(), r"^MATCH \d+$")
        self.assertFalse(any(" write " in c or " erase " in c or " commit " in c for c in console.commands))

    def test_publish_falls_back_when_not_advertised(self):
        update, kind, device = self.device("compact")
        console = Console(device, kind, capable=False)
        answers = []

        def command(text, timeout=30):
            console.commands.append(text)
            if "transfer-capabilities" in text:
                return "ERR Unknown native update command"
            answers.append(text)
            return "OK Rebooting" if " commit " in text else "OK"
        console.command = command
        native_update.publish(console, update, lambda _: None)
        device.kill()
        self.assertTrue(any(" erase " in c for c in answers) and any(" write " in c for c in answers))
        self.assertTrue(any(" commit " in c for c in answers))

    def test_publish_falls_back_when_begin_is_refused(self):
        update, kind, device = self.device("compact")
        console = Console(device, kind + 1)  # Wrong kind: the keyboard refuses BEGIN before any data.
        legacy = []
        original = console.command

        def command(text, timeout=30):
            if " erase " in text or " write " in text or " commit " in text:
                legacy.append(text)
                return "OK Rebooting" if " commit " in text else "OK"
            return original(text, timeout)
        console.command = command
        native_update.publish(console, update, lambda _: None)
        device.kill()
        self.assertTrue(legacy and legacy[-1].startswith("native_install update commit"))

    def test_disabled_or_deviceless_console_never_probes(self):
        update, kind, device = self.device("compact")
        console = Console(device, kind)
        console.binary_transfer = False
        with self.assertRaises(nt.TransferUnavailable):
            nt.probe(console)
        device.kill()

        class Bare:
            commands = []

            def command(self, text, timeout=30):
                self.commands.append(text)
        bare = Bare()
        with self.assertRaises(nt.TransferUnavailable):
            nt.probe(bare)
        self.assertEqual(bare.commands, [])

class ScriptDevice:
    """A keyboard that follows a script, to make protocol corner cases deterministic."""

    def __init__(self, handler):
        self.handler = handler
        self.decoder = nt.Decoder()
        self.queue = []
        self.seen = []

    def write(self, data):
        for frame in self.decoder.push(bytes(data[1:])):
            self.seen.append(frame)
            self.queue += [nt.encode(t, frame[1], offset, payload).ljust(63, b"\0") for t, offset, payload in self.handler(frame)]
        return len(data)

    def read(self, size, timeout):
        if self.queue:
            return list(self.queue.pop(0))
        time.sleep(min(timeout / 1000, 0.0005))
        return []


class FakeUpdate:
    segments = {2: bytes(range(256)) * 4}  # 1 KiB of non-blank data
    metadata = 2
    payload = bytes(64)


def ack(status, offset, credit=8):
    return (nt.ACK, offset, bytes([status]) + credit.to_bytes(2, "little"))


class ScriptedProtocolTests(unittest.TestCase):
    def run_script(self, handler, **options):
        device = ScriptDevice(handler)
        console = Console(device, 0)
        nt.upload(console, FakeUpdate, lambda _: None, **options)
        return device

    def test_commit_waits_for_a_real_verified_ack(self):
        ends = []

        def handler(frame):
            kind, _, offset, payload = frame
            if kind == nt.BEGIN:
                return [ack(nt.STATE_OPEN, 0)]
            if kind == nt.DATA:
                return [ack(nt.STATE_RECEIVING, offset + len(payload))]
            if kind == nt.END:
                ends.append(offset)
                if len(ends) == 1:
                    return [(nt.NAK, offset, bytes([nt.NAK_SINK_BUSY]))]
                if len(ends) == 2:
                    return [ack(nt.STATE_RECEIVING, offset)]  # A straggler ACK is not verification.
                return [ack(nt.STATE_VERIFIED, offset)]
            return [(nt.DONE, offset, b"")]
        device = self.run_script(handler)
        order = [f[0] for f in device.seen]
        self.assertEqual(len(ends), 3)
        self.assertEqual(order.count(nt.COMMIT), 1)
        self.assertGreater(order.index(nt.COMMIT), len(order) - 1 - order[::-1].index(nt.END))

    def test_lost_frame_is_recovered_by_nak_not_by_the_stall_timer(self):
        state = {"dropped": False, "next": 0}

        def handler(frame):
            kind, _, offset, payload = frame
            if kind == nt.BEGIN:
                return [ack(nt.STATE_OPEN, 0, 4)]
            if kind == nt.DATA:
                if offset == 98 and not state["dropped"]:
                    state["dropped"] = True
                    return []
                if offset != state["next"]:
                    return [(nt.NAK, state["next"], bytes([nt.NAK_OUT_OF_ORDER]))]
                state["next"] = offset + len(payload)
                return [ack(nt.STATE_RECEIVING, state["next"], 4)]
            if kind == nt.END:
                return [ack(nt.STATE_VERIFIED, offset)]
            return [(nt.DONE, offset, b"")]
        started = time.monotonic()
        device = self.run_script(handler, window=4, stall_seconds=30.0)
        self.assertLess(time.monotonic() - started, 5.0)  # The stall timer would take 30 s.
        data_frames = [f for f in device.seen if f[0] == nt.DATA]
        self.assertLess(len(data_frames), 40)  # One rewind, not a retransmission storm.

    def run_guarded(self, handler, seconds=10, **options):
        """Run an upload but fail the test (instead of hanging) if it does not return."""
        import signal
        device = ScriptDevice(handler)

        def timeout(*_):
            raise AssertionError("upload did not return: no stall detection")
        signal.signal(signal.SIGALRM, timeout)
        signal.alarm(seconds)
        try:
            nt.upload(Console(device, 0), FakeUpdate, lambda _: None, **options)
        finally:
            signal.alarm(0)
        return device

    def test_endless_busy_naks_are_not_progress(self):
        class Flood(ScriptDevice):
            """A keyboard that answers every read with a busy NAK, whatever the host sent."""

            def read(self, size, timeout):
                if not self.queue:
                    self.queue.append(nt.encode(nt.NAK, self.session, 0, bytes([nt.NAK_SINK_BUSY])).ljust(63, b"\0"))
                return super().read(size, timeout)

        def handler(frame):
            return [ack(nt.STATE_OPEN, 0)] if frame[0] == nt.BEGIN else []
        device = Flood(handler)
        original = device.write

        def write(data):  # The flood frames carry the session the client chose.
            for frame in nt.Decoder().push(bytes(data[1:])):
                device.session = frame[1]
            return original(data)
        device.write = write
        import signal

        def timeout(*_):
            raise AssertionError("upload did not return: NAKs were treated as progress")
        signal.signal(signal.SIGALRM, timeout)
        signal.alarm(10)
        try:
            with self.assertRaisesRegex(RuntimeError, "stopped responding"):
                nt.upload(Console(device, 0), FakeUpdate, lambda _: None, stall_seconds=0.05, retries=3)
        finally:
            signal.alarm(0)
    def test_a_nak_offset_beyond_what_was_sent_is_ignored(self):
        state = {"next": 0, "lied": False}

        def handler(frame):
            kind, _, offset, payload = frame
            if kind == nt.BEGIN:
                return [ack(nt.STATE_OPEN, 0, 4)]
            if kind == nt.DATA:
                if not state["lied"]:
                    state["lied"] = True
                    return [(nt.NAK, 500, bytes([nt.NAK_SINK_BUSY]))]  # In range but impossible: far beyond anything sent yet.
                if offset != state["next"]:
                    return [(nt.NAK, state["next"], bytes([nt.NAK_OUT_OF_ORDER]))]
                state["next"] = offset + len(payload)
                return [ack(nt.STATE_RECEIVING, state["next"], 4)]
            if kind == nt.END:
                total = len(nt.records(FakeUpdate))
                if state["next"] != total:
                    return [(nt.ERROR, 0, bytes([4]))]  # The client claimed completion too early.
                return [ack(nt.STATE_VERIFIED, offset)]
            return [(nt.DONE, offset, b"")]
        self.run_guarded(handler, window=4, stall_seconds=0.2)
        self.assertEqual(state["next"], len(nt.records(FakeUpdate)))

    def test_busy_nak_carrying_the_receivers_offset_resumes_exactly_there(self):
        state = {"next": 0, "busy_once": True}
        seen = []

        def handler(frame):
            kind, _, offset, payload = frame
            if kind == nt.BEGIN:
                return [ack(nt.STATE_OPEN, 0, 4)]
            if kind == nt.DATA:
                seen.append(offset)
                if offset == 49 and state["busy_once"]:
                    state["busy_once"] = False
                    return [(nt.NAK, state["next"], bytes([nt.NAK_SINK_BUSY]))]  # Correct: receiver offset, not 49.
                if offset != state["next"]:
                    return [(nt.NAK, state["next"], bytes([nt.NAK_OUT_OF_ORDER]))]
                state["next"] = offset + len(payload)
                return [ack(nt.STATE_RECEIVING, state["next"], 4)]
            if kind == nt.END:
                return [ack(nt.STATE_VERIFIED, offset)] if state["next"] == len(nt.records(FakeUpdate)) else [(nt.ERROR, 0, bytes([4]))]
            return [(nt.DONE, offset, b"")]
        self.run_guarded(handler, window=4, stall_seconds=5.0)
        self.assertEqual(state["next"], len(nt.records(FakeUpdate)))

    def test_error_frame_aborts_and_is_reported(self):
        def handler(frame):
            kind, _, offset, payload = frame
            if kind == nt.BEGIN:
                return [ack(nt.STATE_OPEN, 0)]
            if kind == nt.DATA:
                return [(nt.ERROR, offset, bytes([5]))]
            return []
        device = ScriptDevice(handler)
        with self.assertRaisesRegex(RuntimeError, "checksum"):
            nt.upload(Console(device, 0), FakeUpdate, lambda _: None)
        self.assertEqual(device.seen[-1][0], nt.ABORT)

if __name__ == "__main__":
    unittest.main()
