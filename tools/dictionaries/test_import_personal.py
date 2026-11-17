"""Round-trip the personal dictionary import against the real firmware store.

Builds tools/tests/test_user_dictionary_import.cc (the store in
src/steno_squid/user_dictionary_log.h on a fake NOR flash), then uploads images
from segment_image.py through the same import_personal.py code the host tool
runs, and checks what the store holds afterwards.

    python3 tools/dictionaries/test_import_personal.py
"""

import json
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import import_personal
from import_personal import Console, NoSpace, line_command, parse_dictionary
from segment_image import Image

REPO = Path(__file__).resolve().parents[2]
BINARY = {}


def build_harness(folder):
    compiler = shutil.which("g++") or shutil.which("clang++") or shutil.which("c++")
    if not compiler:
        raise SystemExit("A C++ compiler is needed to build the store harness.")
    exe = Path(folder) / "test_user_dictionary_import"
    subprocess.run([compiler, "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
                    "-I", str(REPO / "src/steno_squid"),
                    str(REPO / "tools/tests/test_user_dictionary_import.cc"),
                    "-o", str(exe)], check=True)
    return exe


def key_hex(strokes):
    return struct.pack(f"<{len(strokes)}I", *strokes).hex()


def make_entries(count, seed=7):
    """Multi-stroke outlines, shared translations, long texts and non-ASCII text."""
    rng = random.Random(seed)
    shared = [f"shared {n}" for n in range(300)]
    words = ["ça va", "naïve café", "日本語のテキスト", "emoji \U0001F991", "ünïcödé", "plain"]
    entries = {}
    while len(entries) < count:
        n = len(entries)
        strokes = tuple(rng.randrange(1, 1 << 23) for _ in range(rng.choice((1, 1, 2, 3, 5, 16))))
        kind = n % 10
        if kind < 3:
            text = rng.choice(shared)
        elif kind == 3:
            text = rng.choice(words) + f" {n}"
        elif kind == 4 and n % 100 == 4:
            text = ("long text " * 300)[:rng.randrange(1500, 2000)] + f" {n}"
        else:
            text = f"word{n}"
        entries[strokes] = text.encode("utf-8")
    entries[(1, 2, 3)] = ("é" * 1023 + "z").encode("utf-8")  # exactly 2,047 bytes
    return entries


class Harness(Console):
    """import_personal's Console, talking to the C++ store instead of USB HID."""

    def __init__(self, segments=175):
        self.proc = subprocess.Popen([str(BINARY["exe"]), str(segments)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE)
        self.sent = []

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=60)
        self.proc.stdout.close()
        assert self.proc.returncode == 0, "the store harness failed an internal check"

    def write(self, text):
        self.proc.stdin.write(text.encode("utf-8") + b"\n")
        self.proc.stdin.flush()

    def read(self):
        reply = self.proc.stdout.readline()
        if not reply:
            raise RuntimeError("the store harness exited")
        return reply.decode("utf-8").rstrip("\r\n")

    def command(self, text, timeout=30):
        self.sent.append(text)
        self.write(text)
        return self.read()

    def status(self):
        return import_personal.read_status(self)

    def verify(self):
        return json.loads(self.command("user_dictionary_verify"))

    def find(self, strokes):
        reply = self.command(f"find {key_hex(strokes)}")
        return None if reply == "MISSING" else bytes.fromhex(reply.split()[1].replace("-", ""))

    def reverse(self, text):
        reply = self.command("reverse " + (text.hex() or "-")).split()
        assert reply[0] == "KEYS"
        return set(reply[1:])

    def export(self):
        self.write("export")
        rows = {}
        while True:
            line = self.read()
            if line == "END":
                return rows
            key, text = line.split()
            rows[key] = bytes.fromhex(text.replace("-", ""))

    def reopen(self):
        assert self.command("reopen") == "OK"


class ImportTest(unittest.TestCase):
    def harness(self, segments=175):
        console = Harness(segments)
        self.addCleanup(console.close)
        return console

    def check_matches(self, console, entries, strict=True):
        status = console.status()
        self.assertEqual(status["state"], "ready")
        self.assertEqual(status["entries"], len(entries))
        if strict:  # Rejected uploads are counted too.
            self.assertEqual(status["integrity_errors"], 0)
        report = console.verify()
        self.assertEqual(report, {"live": len(entries), "bad_crc": 0, "unreachable": 0})
        for strokes, text in entries.items():
            self.assertEqual(console.find(strokes), text, strokes)
        exported = console.export()
        self.assertEqual(exported, {key_hex(k): v for k, v in entries.items()})

    def test_round_trip_five_thousand_entries(self):
        entries = make_entries(5000)
        console = self.harness()
        import_personal.upload(console, entries)
        self.check_matches(console, entries)
        # Every translation finds every outline that shares it.
        by_text = {}
        for strokes, text in entries.items():
            by_text.setdefault(text, set()).add(key_hex(strokes))
        shared = [text for text, keys in by_text.items() if len(keys) > 1]
        self.assertGreater(len(shared), 100)
        for text in shared[:50] + [t for t in by_text if t.isascii() is False][:20]:
            self.assertEqual(console.reverse(text), by_text[text])
        self.assertEqual(console.reverse(b"no such translation"), set())
        # A power cycle keeps all of it, and the store takes ordinary edits.
        console.reopen()
        self.check_matches(console, entries)
        first = next(iter(entries))
        self.assertEqual(console.command(f"add {key_hex(first)} {b'changed'.hex()}"), "OK")
        self.assertEqual(console.command(f"add {key_hex((9, 9))} {'new é'.encode().hex()}"), "OK")
        entries[first] = b"changed"
        entries[(9, 9)] = "new é".encode()
        self.check_matches(console, entries)

    def test_flipped_bit_fails_the_import_and_changes_nothing(self):
        before = make_entries(300, seed=3)
        console = self.harness()
        import_personal.upload(console, before)
        after = make_entries(5000, seed=11)
        image = Image(after)
        reply = json.loads(console.command(
            f"user_dictionary_import begin {image.record_segments} "
            f"{image.forward_segments} {image.reverse_segments}"))
        good = image.build(reply["first_sequence"], reply["fwd_epoch"], reply["rev_epoch"],
                           reply["segments"])
        records, forward = image.record_segments, image.forward_segments
        first = struct.unpack_from("<I", good[0], 16)[0]
        text_at = 16 + 8 + 4 * (((first >> 24) & 31) + 1)
        occupied = next(o for o in range(16, len(good[records]), 4)
                        if good[records][o:o + 4] != b"\xff" * 4)
        # (segment, byte, xor): a record's size check, a stroke, its text, its DEAD
        # flag, a segment header's magic and sequence, an occupied forward slot, and
        # an empty reverse slot that gets filled.
        spots = [(0, 16, 0x08), (0, 24, 0x01), (0, text_at, 0x10), (0, 19, 0x20),
                 (0, 0, 0x80), (0, 8, 0x02), (records, occupied + 1, 0x04),
                 (records + forward, 0x8000, 0x00)]
        for segment, offset, flip in spots:
            with self.subTest(segment=segment, offset=offset, flip=flip):
                bad = [bytearray(s) for s in good]
                if flip:
                    bad[segment][offset] ^= flip
                else:
                    bad[segment] = bytearray(good[segment].ljust(0x10000, b"\xff"))
                    bad[segment][offset] = 0
                with self.assertRaises(RuntimeError):
                    import_personal.send_segments(console, [bytes(s) for s in bad])
                self.assertEqual(console.status()["state"], "ready")
                self.assertEqual(console.status()["entries"], len(before))
                self.assertEqual(console.verify()["live"], len(before))
                # The abort left the keyboard able to import again.
                reply = json.loads(console.command(
                    f"user_dictionary_import begin {image.record_segments} "
                    f"{image.forward_segments} {image.reverse_segments}"))
                good = image.build(reply["first_sequence"], reply["fwd_epoch"],
                                   reply["rev_epoch"], reply["segments"])
        # The unmodified image still commits.
        import_personal.send_segments(console, good)
        self.check_matches(console, after, strict=False)

    def test_no_space_without_replace_then_replace(self):
        small = make_entries(100, seed=5)
        large = make_entries(1900, seed=9)
        # Two record segments and a table each: too big beside `small` in 10 segments,
        # small enough for all of them once the old dictionary is erased.
        self.assertEqual(Image(large).total_segments, 4)
        console = self.harness(10)
        import_personal.upload(console, small)
        self.check_matches(console, small)
        with self.assertRaises(NoSpace):
            import_personal.upload(console, large)
        self.check_matches(console, small)  # Refused: nothing changed.
        backups = []

        def write_backup():
            backups.append(True)
            return "backup.json"

        # bulk_import's own size check uses the full region; bypass it for 10 segments.
        status = console.status()
        status["free_segments"] += 200
        import_personal.bulk_import(console, status, large, write_backup)
        self.assertEqual(backups, [True])
        self.check_matches(console, large)
        for strokes in set(small) - set(large):
            self.assertIsNone(console.find(strokes))

    def test_small_dictionary_fits_beside_the_current_one(self):
        first = make_entries(200, seed=1)
        second = make_entries(300, seed=2)
        console = self.harness(12)
        import_personal.upload(console, first)
        import_personal.upload(console, second)  # No replace needed, no backup.
        self.check_matches(console, second)

    def test_parsing_and_line_commands(self):
        rows = parse_dictionary('{"K":"new","K/P":"ça va","-S":"{^s}"}')
        self.assertEqual(len(rows), 3)
        self.assertIn("ça va".encode(), rows.values())
        self.assertEqual(line_command("K/P", "ça va".encode()), "add_user_entry K/P ça va")
        self.assertIsNone(line_command("K", b" padded"))
        self.assertIsNone(line_command("K", b"two\nlines"))
        with self.assertRaises(ValueError):
            parse_dictionary('{"K":"a","K":"b"}')


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as folder:
        BINARY["exe"] = build_harness(folder)
        unittest.main()
