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
from test_native_stage import fixture, write_plan, ROOT

sys.path.insert(0, str(ROOT / "tools/dictionaries"))
from build_native_image import SEGMENT, catalog_header
from compile_dict import parse_outline
from native_install import check_image
from native_recovery import FORMAT, digest, pack, reconstruct, save_atomic, unpack
from native_snapshot import CompactImage, compile_snapshot, merge_snapshots, plan_change, plan_named_snapshot
from native_update import Snapshot, backup_all, effective_entries, encode_record, Edit, export_dictionary, overlay_usage, plan, row_name


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
        if command == "native_install update journal-usage-capabilities":
            return "ERR Unsupported"
        if command in ("native_install update begin backup", "native_install update begin metadata"):
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
        # Model firmware replies separately from the host fallback, including a
        # repeated (block, dictionary) pair received in successive slices.
        expected_usage = overlay_usage(Console(image, snapshot.payload))
        live = sorted(snapshot.entries.values(), key=lambda entry: entry.address)
        assert live

        class UsageConsole(Console):
            def command(self, command, timeout=30):
                if command == "native_install update journal-usage-capabilities":
                    return "OK USAGE2"
                if command.startswith("native_install update journal-usage "):
                    assert self.active
                    position = int(command.rsplit(" ", 1)[1])
                    if position == len(live):
                        return f"OK USAGE2 {position} {len(live)}"
                    entry = live[position]
                    return (f"OK USAGE2 {position + 1} {len(live)} "
                            f"{entry.address // SEGMENT},{entry.identity},1,"
                            f"{int(entry.operation == 0)},{entry.span}")
                if command.startswith("native_install update read "):
                    assert int(command.split()[-2]) < 2 * SEGMENT
                return super().command(command, timeout)

        assert overlay_usage(UsageConsole(image, snapshot.payload)) == expected_usage
        prior = backup_all(Console(image, snapshot.payload))
        assert prior["format"] == "stenosquid-backup-2" and "capture" not in prior

        class IdentifiedConsole(Console):
            serial = "test-left"

            def command(self, command, timeout=30):
                if command == "native_install status":
                    assert self.active
                    return "NATIVE2 READY 7"
                return super().command(command, timeout)

        identified = backup_all(IdentifiedConsole(image, snapshot.payload))
        assert identified["capture"]["serial"] == "test-left"
        assert identified["capture"]["generation"] == 7 and identified["capture"]["timestamp"].endswith("+00:00")
        assert reconstruct(identified)[1].payload == snapshot.payload
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

        # A valid catalog/CRC cannot make bytes beyond the supported image admissible.
        unsupported = CompactImage(compiled.data + bytes(20), compiled.pointers)
        invalid = plan_named_snapshot(snapshot, protected, "Smoke", unsupported)
        bad_image = bytearray(image)
        for segment, block in invalid.segments.items():
            start = segment * SEGMENT
            bad_image[start:start + len(block)] = block
        start = SEGMENT + 32
        bad_image[start:start + len(invalid.payload)] = invalid.payload
        reject(backup_all, Console(bad_image, invalid.payload))
        reject(export_dictionary, Console(bad_image, invalid.payload), "Smoke")

        bad_bundle = deepcopy(backup)
        blob = unpack(bad_bundle["extents"][-1]) + bytes(20)
        payload = bytearray(unpack(bad_bundle["catalog"]))
        struct.pack_into("<II", payload, len(payload) - 20, len(blob), zlib.crc32(blob))
        bad_bundle["extents"][-1] = pack(blob)
        bad_bundle["catalog"] = pack(payload)
        bad_bundle["sha256"] = digest(bad_bundle)
        reject(reconstruct, bad_bundle)

        old_bundle = deepcopy(backup)
        old_payload = bytearray(unpack(old_bundle["catalog"]))
        forward = next(i for i, extent in enumerate(restored.extents) if extent[4] == 3)
        struct.pack_into("<Q", old_payload, 32 + len(restored.rows) * 96 + 24 * forward + 16, 0)
        old_bundle["catalog"] = pack(old_payload)
        old_bundle["sha256"] = digest(old_bundle)
        reject(reconstruct, old_bundle)
        reject(lambda: Snapshot(lambda at, size: old_payload[at:at + size], 0,
                                len(old_payload), zlib.crc32(old_payload), metadata_only=True))

        protected = {0, 1, 255}
        for segment, count, *_ in restored.extents:
            protected.update(range(segment, segment + count))
        identity = snapshot.next_id
        effective = compile_snapshot("Smoke", {parse_outline(outline): text.encode() for outline, text in exported.items()})
        rename = plan(restored, protected, rename=("Smoke", "Renamed"))
        deletion = plan_change(restored, protected, identity)
        consolidated = plan_change(restored, protected, identity, effective, consolidate=True)
        missing = deepcopy(restored)
        del missing.entries[2, (6,)]
        unsafe = plan_change(missing, protected, identity, effective, consolidate=True)

        def cold(update):
            changed = bytearray(decoded)
            for segment, block in update.segments.items():
                assert segment not in protected
                start = segment * SEGMENT
                changed[start:start + len(block)] = block
            current = Console(changed, update.payload)
            root = SEGMENT + 32
            changed[root:root + len(update.payload)] = update.payload
            return current

        assert export_dictionary(cold(rename), "Renamed") == exported
        reject(export_dictionary, cold(deletion), "Smoke")
        merged = cold(consolidated)
        assert export_dictionary(merged, "Smoke") == exported
        assert overlay_usage(merged)[identity]["count"] == 0
        lean_backup = backup_all(merged)
        lean_image, _ = reconstruct(lean_backup)
        assert export_dictionary(Console(lean_image, consolidated.payload), "Smoke") == exported
        with patch("native_update.read_flash") as no_device:
            assert effective_entries(restored, "Smoke", lambda at, size: decoded[at:at + size]) == exported
            no_device.assert_not_called()

        binary_image = path / "offline.bin"
        binary_image.write_bytes(decoded)
        files = []
        for name, candidate in (("rename", rename), ("delete", deletion),
                                ("consolidate", consolidated), ("unsafe", unsafe)):
            file = path / f"{name}.stage"
            write_plan(file, candidate)
            files.append(file)
        higher = compile_snapshot("Higher", {kat: b"upper cat", bob: b"Bob restored by higher priority"})
        both = cold(plan_named_snapshot(restored, protected, "Higher", higher))
        # Fill the remaining slots with journal-only rows to exercise a merge
        # that cannot allocate a new dictionary ID or use a temporary slot.
        current = bytearray(both.payload)
        next_id, row_count = struct.unpack_from("<IH", current)
        extra_rows = []
        for dictionary_id in range(next_id, next_id + 16 - row_count):
            row = bytearray(96)
            label = f"Journal {dictionary_id}".encode()
            struct.pack_into("<IIH", row, 0, dictionary_id, 1, 65535)
            row[12] = len(label)
            row[16:16 + len(label)] = label
            extra_rows.append(row)
        end = 32 + 96 * row_count
        current[end:end] = b"".join(extra_rows)
        struct.pack_into("<IH", current, 0, next_id + len(extra_rows), 16)
        both.image[SEGMENT + 32:SEGMENT + 32 + len(current)] = current
        both = Console(both.image, bytes(current))
        before_merge, merge_view = reconstruct(backup_all(both))
        assert len(merge_view.rows) == 16
        published = []

        class MergeConsole(Console):
            serial = "test-left"

            def command(self, command, timeout=30):
                if command == "native_install update snapshot-merge-capabilities":
                    return "OK MERGE1 16"
                if command == "native_install update begin snapshot-merge 9 8":
                    answer = super().command("native_install update begin backup", timeout)
                    occupied = {0, 1, 255}
                    for start, count, *_ in merge_view.extents:
                        occupied.update(range(start, start + count))
                    words = [sum(1 << (segment % 32) for segment in occupied
                                 if segment // 32 == index) for index in range(8)]
                    return answer[:-64] + "".join(f"{word:08x}" for word in words)
                return super().command(command, timeout)

        merge_console = MergeConsole(before_merge, merge_view.payload)
        with patch("native_snapshot.publish", side_effect=lambda console, update, progress: published.append(update)):
            preview = merge_snapshots(merge_console, 9, 8, progress=lambda _: None)
            assert preview["entries"] == 4 and preview["duplicates"] == 1
            assert not published and not merge_console.active
            try:
                merge_snapshots(merge_console, 9, 8, expected="stale", progress=lambda _: None)
            except ValueError as error:
                assert "Preview the merge again" in str(error)
            else:
                raise AssertionError("A stale merge preview was accepted")
            assert not published and not merge_console.active
            assert merge_snapshots(merge_console, 9, 8, expected=preview["token"],
                                   progress=lambda _: None) == "Higher"
        merged_plan, = published
        merged_image = bytearray(before_merge)
        for segment, block in merged_plan.segments.items():
            start = segment * SEGMENT
            merged_image[start:start + len(block)] = block
        merged_image[SEGMENT + 32:SEGMENT + 32 + len(merged_plan.payload)] = merged_plan.payload
        assert struct.unpack_from("<IH", merged_plan.payload) == (merge_view.next_id, 15)
        merged_console = Console(merged_image, merged_plan.payload)
        assert export_dictionary(merged_console, "Higher") == exported | {
            "KAT": "upper cat", "PWOB": "Bob restored by higher priority"}
        reject(export_dictionary, merged_console, "Smoke")
        merge_image_file, merge_plan_file = path / "merge-before.bin", path / "merge.stage"
        merge_image_file.write_bytes(before_merge)
        write_plan(merge_plan_file, merged_plan)
        files.extend([merge_image_file, merge_plan_file])

        binary = path / "validate"
        subprocess.run([*compiler(), "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-I", str(ROOT / "tools/storage"),
                        str(ROOT / "tools/tests/test_native_recovery.cc"), "-o", str(binary)], check=True)
        subprocess.run([str(binary), str(binary_image), *map(str, files)], check=True)

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
