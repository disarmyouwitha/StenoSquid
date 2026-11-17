"""Tests for stenosquid.py against a fake keyboard that speaks the serial protocol.

No hardware and no pyserial needed. Run from anywhere:

    python3 tools/dictionaries/test_stenosquid.py

The fake keyboard answers like the firmware: it reports each image's expected
size and CRC, accepts the preflight and write passes in 512-byte chunks, and
refuses to erase anything the script did not ask it to. Time is faked, so the
silent-firmware cases do not wait. Every test also fails if the script ever
asks a question, because project scripts must never prompt.
"""

from contextlib import nullcontext
import builtins
import collections
import contextlib
import io
import sys
import tempfile
import types
import unittest
import zlib
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.modules.setdefault("serial", types.SimpleNamespace(Serial=object))
import stenosquid  # noqa: E402

DIAG_REPLIES = {
    b"S": ["BUILD abc", "[b1 2.0] noise", "FLASH mapped=1", "END", "after"],
    b"D": ["LOG BEGIN 10 bytes", "[b1 0.1] x", "LOG END"],
    b"Z": ["CLEARED log and counters"],
    b"R": ["RAIL enable level high", "RAIL END"],
    b"T": ["BENCH cycle 1 ok", "BENCH END"],
}


def make_image(seed, words=300):
    """A word-aligned JSC4 image of a few kilobytes, different per seed."""
    body = bytes((seed * 31 + i * 7) & 0xFF for i in range(words * 4 - 4))
    return b"JSC4" + body


class FakeTime:
    """Advances on every look at the clock, so waiting for a silent keyboard is instant."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        self.now += 0.25
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Keyboard:
    def __init__(self, *args, **kwargs):
        self.out = collections.deque()
        self.sent = []

    def write(self, data):
        self.sent.append(data)
        for line in DIAG_REPLIES.get(data, []):
            self.out.append((line + "\r\n").encode())

    def readline(self):
        return self.out.popleft() if self.out else b""

    def reset_input_buffer(self):
        self.out.clear()

    def flush(self):
        pass


class UploadTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        root = Path(self.dir.name)
        self.images = {"LAPWING": make_image(1), "PLOVER": make_image(2), "EXTRAS": make_image(3)}
        self.paths = {}
        for name, data in self.images.items():
            self.paths[name] = root / f"{name.lower()}.bin"
            self.paths[name].write_bytes(data)

    def run_script(self, keyboard, *args):
        class FakeSerial:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return keyboard

            def __exit__(self, *a):
                return False

        def no_prompt(*a, **k):
            raise AssertionError("the script asked a question")

        out = io.StringIO()
        code = None
        with contextlib.redirect_stdout(out), \
                mock.patch.object(stenosquid, "Serial", FakeSerial), \
                mock.patch.object(stenosquid, "time", FakeTime()), \
                mock.patch.object(stenosquid, "DEFAULT_IMAGES", self.paths), \
                mock.patch.object(builtins, "input", no_prompt), \
                mock.patch.object(sys, "argv", ["stenosquid.py", "COM1", *args]):
            try:
                stenosquid.main()
            except SystemExit as exit_:
                code = exit_.code
        return code, out.getvalue()

    def keyboard(self, names=("LAPWING", "PLOVER", "EXTRAS"), installed=(), damaged=()):
        return Keyboard({n: self.images[n] for n in names}, installed, damaged)

    def programmed(self, keyboard):
        return [n for n, a in keyboard.log if a == "program"]


    def test_default_install_uses_hid_and_prepares_images_before_writing(self):
        import import_personal

        self.paths["EXTRAS"].unlink()
        transfers = []
        verified = {"LAPWING"}
        active = None

        def command(text, timeout=30):
            nonlocal active
            _, operation, *args = text.split()
            if operation == "status":
                state = "READY" if len(verified) == len(self.images) else "INSTALL"
                return "\n".join([f"1 {state}"] + [
                    f"{name} {len(image)} {zlib.crc32(image)} {int(name in verified)}"
                    for name, image in self.images.items()
                ])
            if operation == "begin":
                active, = args
                self.assertNotIn(active, verified)
                self.assertTrue(self.paths["EXTRAS"].exists())
                transfers.append((active, bytearray()))
            elif operation == "data":
                offset, chunk = args
                self.assertEqual(int(offset), len(transfers[-1][1]) % len(self.images[active]))
                transfers[-1][1].extend(bytes.fromhex(chunk))
            elif operation == "commit":
                self.assertEqual(transfers[-1][1], self.images[active] * 2)
                verified.add(active)
            else:
                self.fail(f"Unexpected command: {text}")
            return "OK"

        console = mock.Mock()
        console.command.side_effect = command
        with mock.patch.object(import_personal, "open_console", return_value=nullcontext(console)) as open_hid, \
                mock.patch.object(stenosquid, "Serial", side_effect=AssertionError("opened serial")), \
                mock.patch.object(stenosquid, "DEFAULT_IMAGES", self.paths), \
                mock.patch.object(stenosquid, "prepare_base") as prepare, \
                mock.patch.object(sys, "argv", ["stenosquid.py", "--install", "--serial", "S1"]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            prepare.side_effect = lambda name, _: self.paths[name].write_bytes(self.images[name])
            stenosquid.main()
        open_hid.assert_called_once_with("S1")
        self.assertEqual([name for name, _ in transfers], ["PLOVER", "EXTRAS"])
        self.assertIn("installed and ready", output.getvalue())

    def test_hid_install_conflict_gives_scoped_repair_command(self):
        image = self.images["PLOVER"]
        console = mock.Mock()
        console.command.side_effect = [
            f"1 INSTALL\nPLOVER {len(image)} {zlib.crc32(image)} 0",
            "OK", "ERR Base region holds different data. No sectors were erased", "OK",
        ]
        with mock.patch.object(stenosquid, "DEFAULT_IMAGES", self.paths), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "stenosquid.py --repair --base plover"):
                stenosquid.install_hid(console)
        self.assertEqual(console.command.call_args_list[-1], mock.call("base_install abort", timeout=2))
        self.assertFalse(any(" repair " in call.args[0] for call in console.command.call_args_list))

    def test_hid_repair_selects_one_image_and_leaves_old_cache_intact(self):
        import import_personal

        image = self.images["PLOVER"]
        old_image = make_image(19)
        self.paths["PLOVER"].write_bytes(old_image)
        console = mock.Mock()

        def command(text, timeout=30):
            if text == "base_install status":
                state = any(call.args[0] == "base_install commit" for call in console.command.call_args_list)
                return f"1 INSTALL\nPLOVER {len(image)} {zlib.crc32(image)} {int(state)}\nEXTRAS 4 0 0"
            return "OK"

        console.command.side_effect = command
        with mock.patch.object(import_personal, "open_console", return_value=nullcontext(console)), \
                mock.patch.object(stenosquid, "DEFAULT_IMAGES", self.paths), \
                mock.patch.object(stenosquid, "prepare_base") as prepare, \
                mock.patch.object(sys, "argv", ["stenosquid.py", "--repair", "--base", "plover"]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            prepare.side_effect = lambda name, expected, path: path.write_bytes(image)
            stenosquid.main()
        commands = [call.args[0] for call in console.command.call_args_list]
        self.assertEqual(commands[1], "base_install repair PLOVER")
        self.assertNotIn("base_install repair EXTRAS", commands)
        self.assertEqual(self.paths["PLOVER"].read_bytes(), old_image)
        self.assertIn("remaining bases", output.getvalue())

    def test_hid_repair_all_checks_healthy_bases_and_repairs_each_failure(self):
        import import_personal

        for args in (["--repair"], ["--repair", "--base", "all"]):
            with self.subTest(args=args):
                verified = {"LAPWING", "PLOVER"}  # Plover has a stale verification flag.
                corrupted = {"PLOVER"}
                repaired = []
                console = mock.Mock()

                def command(text, timeout=30):
                    _, operation, *parts = text.split()
                    if operation == "status":
                        state = "READY" if len(verified) == len(self.images) else "INSTALL"
                        return "\n".join([f"1 {state}"] + [
                            f"{name} {len(image)} {zlib.crc32(image)} {int(name in verified)}"
                            for name, image in self.images.items()
                        ])
                    if operation == "verify":
                        name, = parts
                        return "MISMATCH 0" if name in corrupted else f"OK {zlib.crc32(self.images[name])}"
                    if operation == "repair":
                        repaired.extend(parts)
                    if operation == "commit":
                        verified.add(repaired[-1])
                        corrupted.discard(repaired[-1])
                    return "OK"

                console.command.side_effect = command
                with mock.patch.object(import_personal, "open_console", return_value=nullcontext(console)), \
                        mock.patch.object(stenosquid, "DEFAULT_IMAGES", self.paths), \
                        mock.patch.object(sys, "argv", ["stenosquid.py", *args]), \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    stenosquid.main()
                self.assertEqual(repaired, ["PLOVER", "EXTRAS"])
                self.assertIn("LAPWING: already installed", output.getvalue())
                self.assertIn("installed and ready", output.getvalue())

    def test_verify_and_status_default_to_hid(self):
        import import_personal

        for action in ("--status", "--verify"):
            console = mock.Mock()
            console.command.side_effect = ["1 READY\nLAPWING 12 1 1", "OK 1"]
            with mock.patch.object(import_personal, "open_console", return_value=nullcontext(console)), \
                    mock.patch.object(stenosquid, "Serial", side_effect=AssertionError("opened serial")), \
                    mock.patch.object(sys, "argv", ["stenosquid.py", action]), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                stenosquid.main()
            self.assertIn("LAPWING:", output.getvalue())
            self.assertEqual(console.command.call_count, 2 if action == "--verify" else 1)

    def test_verify_hid_reports_bad_checksum(self):
        console = mock.Mock()
        console.command.side_effect = ["1 READY\nLAPWING 12 1 1", "MISMATCH 0"]
        with contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as result:
                stenosquid.inspect_hid(console, "ALL", verify=True)
        self.assertEqual(result.exception.code, 1)
        self.assertIn("FAILED", output.getvalue())

    def test_diagnostics_report_uses_one_detected_port(self):
        port = object()
        with mock.patch.object(stenosquid, "find_port", return_value="COM1"), \
                mock.patch.object(stenosquid, "Serial", return_value=nullcontext(port)) as serial, \
                mock.patch.object(stenosquid, "diag") as diag, \
                mock.patch.object(sys, "argv", ["stenosquid.py", "--diag"]), \
                contextlib.redirect_stdout(io.StringIO()):
            stenosquid.main()
        serial.assert_called_once_with("COM1", 115200, timeout=0.25, write_timeout=5, exclusive=True)
        self.assertEqual(diag.call_args_list, [mock.call(port, part)
                                               for part in ("status", "log", "rail", "bench")])


    def test_diag_commands_stop_at_their_end_line(self):
        for what, letter in (("status", b"S"), ("log", b"D"), ("clear", b"Z"),
                             ("rail", b"R"), ("bench", b"T")):
            with self.subTest(what=what):
                kb = self.keyboard()
                code, text = self.run_script(kb, "--diag", what)
                self.assertIsNone(code)
                self.assertEqual(kb.sent, [letter])
                self.assertIn(stenosquid.DIAG[what][1], text)
        # S stops at END, so the line after it stays unread.
        kb = self.keyboard()
        _, text = self.run_script(kb, "--diag", "status")
        self.assertNotIn("after", text)

    def test_diag_without_a_reply_says_which_build_it_needs(self):
        class Silent(Keyboard):
            def write(self, data):
                self.sent.append(data)

        kb = Silent({})
        code, _ = self.run_script(kb, "--diag", "status")
        self.assertIn("CONFIG_ZMK_JAVELIN_DIAG", str(code))

    def test_backup_and_import_dispatch_to_the_personal_dictionary_tool(self):
        import import_personal

        for action, function in (("--backup", "backup_file"), ("--import", "import_file")):
            with mock.patch.object(import_personal, function) as call,                     mock.patch.object(stenosquid, "Serial", side_effect=AssertionError("opened serial")),                     mock.patch.object(sys, "argv", ["stenosquid.py", action, "dictionary.json",
                                                    "--serial", "ABC"]):
                stenosquid.main()
            call.assert_called_once_with(Path("dictionary.json"), "ABC")

    def test_backup_stands_alone_and_uses_hid(self):
        for extra in (["--import", "other.json"], ["--verify"], ["COM1"]):
            with mock.patch.object(sys, "argv", ["stenosquid.py", "--backup", "b.json", *extra]),                     contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as result:
                    stenosquid.main()
            self.assertEqual(result.exception.code, 2)

    def test_diag_stands_alone(self):
        code, _ = self.run_script(self.keyboard(), "--diag", "status", "--verify")
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
