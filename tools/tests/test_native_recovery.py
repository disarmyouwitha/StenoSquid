#!/usr/bin/env python3
"""Snapshot backups, portable offline reconstruction and interrupted publication."""
import base64
from copy import deepcopy
import hashlib
import json
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
import zlib

from hostlib import compiler
from test_native_stage import fixture, ROOT

sys.path.insert(0, str(ROOT / "tools/dictionaries"))
from build_native_image import SEGMENT, catalog_header
from compile_dict import parse_outline
from native_install import check_image
from native_recovery import FORMAT, digest, pack, reconstruct, save_atomic, unpack
from native_restore import restore_all
from native_snapshot import compile_snapshot, plan_named_snapshot
from native_update import backup_all, encode_record, Edit, export_dictionary, overlay_usage, row_name


def reject(call, *args):
    try:
        call(*args)
    except (ValueError, FileExistsError, OSError):
        return
    raise AssertionError(f"{call.__name__} accepted an invalid or incomplete backup")


class Console:
    active = False
    interrupted = False

    def __init__(self, image, payload):
        self.image, self.payload = image, payload

    def command(self, command, timeout=30):
        if command == "get_parameter host_layout":
            return "Mac U.S."
        if command == "native_install update begin backup":
            assert not self.active
            self.active = True
            return f"OK {SEGMENT + 32} {len(self.payload)} {zlib.crc32(self.payload)} " + "0" * 64
        assert self.active
        if command == "native_install update abort":
            self.active = False
            return "OK"
        _, _, verb, offset, count = command.split()
        assert verb == "read"
        offset, count = int(offset), int(count)
        if self.interrupted and offset >= 2 * SEGMENT:
            raise OSError("USB interrupted")
        return "OK " + pack(self.image[offset:offset + count])


def main():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        image, snapshot, protected = fixture(path)
        prior = backup_all(Console(image, snapshot.payload))
        assert prior["format"] == "stenosquid-backup-1"
        kat, bob, tas = (parse_outline(word) for word in ("KAT", "PWOB", "TAS"))
        compiled = compile_snapshot("Smoke", {kat: b"snapshot cat", bob: b"Bob", (8, 16): "bêta".encode()})
        update = plan_named_snapshot(snapshot, protected, "Smoke", compiled)
        for segment, block in update.segments.items():
            start = segment * SEGMENT
            image[start:start + len(block)] = block
        for slot in range(2):
            start = slot * SEGMENT
            body = catalog_header(slot + 3, update.payload) + update.payload
            image[start:start + len(body)] = body
        offset = snapshot.anchor * SEGMENT + 256 + snapshot.used[snapshot.anchor]
        for seq, (outline, op, text) in enumerate([(kat, 0, b"new cat"), (bob, 1, b""), (tas, 0, b"added")],
                                                  snapshot.highwater + 1):
            entry = Edit(offset, snapshot.next_id, op, outline, text)
            record = encode_record(entry, seq)
            image[offset:offset + len(record)] = record
            offset += entry.span

        console = Console(image, update.payload)
        console.interrupted = True
        reject(backup_all, console)
        assert not console.active
        console.interrupted = False
        exported = export_dictionary(console, "Smoke")
        assert exported["KAT"] == "new cat" and exported["TAS"] == "added" and "PWOB" not in exported
        assert "bêta" in exported.values()
        backup = backup_all(console)
        assert not console.active and backup["format"] == FORMAT
        assert backup["host_layout"] == "Mac U.S."
        decoded, restored = reconstruct(json.loads(json.dumps(backup)))
        assert restored.payload == update.payload
        assert [row_name(row) for row in restored.rows][:3] == ["user_dictionary", "Smoke", "Lapwing Commands"]
        assert restored.entries[snapshot.next_id, bob].operation == 1
        assert overlay_usage(console)[snapshot.next_id]["count"] == 3
        assert export_dictionary(Console(decoded, update.payload), "Smoke") == exported

        binary_image = path / "offline.bin"
        binary_image.write_bytes(decoded)
        binary = path / "validate"
        subprocess.run([*compiler(), "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-DSTENO_SQUID_TEST_USER_SNAPSHOTS=1", "-I", str(ROOT / "tools/storage"),
                        str(ROOT / "tools/tests/test_native_recovery.cc"), "-o", str(binary)], check=True)
        subprocess.run([str(binary), str(binary_image)], check=True)

        corrupted = deepcopy(backup)
        corrupted["extents"].pop()
        reject(reconstruct, corrupted)
        corrupted["sha256"] = digest(corrupted)
        reject(reconstruct, corrupted)
        corrupted = deepcopy(backup)
        blob = bytearray(unpack(corrupted["extents"][-1]))
        blob[-1] ^= 16
        corrupted["extents"][-1] = pack(blob)
        corrupted["sha256"] = digest(corrupted)
        reject(reconstruct, corrupted)
        corrupted = deepcopy(backup)
        payload = bytearray(unpack(corrupted["catalog"]))
        struct.pack_into("<H", payload, len(payload) - 24, 2)
        corrupted["catalog"] = base64.b64encode(payload).decode()
        corrupted["sha256"] = digest(corrupted)
        reject(reconstruct, corrupted)
        with patch("native_restore.open_console") as open_console:
            reject(restore_all, None, backup)
            open_console.assert_not_called()
        manifest = dict(format="SQBCAT02", crc32=zlib.crc32(decoded), sha256=hashlib.sha256(decoded).hexdigest())
        reject(check_image, decoded, manifest)

        backup_path = path / "backup.json"

        def interrupted_dump(document, output, **kwargs):
            output.write('{"catalog":')
            raise OSError("Interrupted while exporting")

        reject(save_atomic, backup_path, backup, interrupted_dump)
        assert not backup_path.exists()
        save_atomic(backup_path, backup)
        reject(save_atomic, backup_path, prior)
        assert json.loads(backup_path.read_text()) == backup
        print("Recovery bundle: offline native cold open, export parity, USB and file interruption, corrupt inputs and erase gate passed")


if __name__ == "__main__":
    main()
