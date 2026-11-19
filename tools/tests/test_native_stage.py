#!/usr/bin/env python3
"""Exercise the actual planner, staged store and every torn-write boundary."""

from copy import copy
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import zlib

from hostlib import compiler

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/storage"))
from build_native_image import build, catalog_header, SEGMENT, SOURCES, write_journal_header
from native_update import Edit, Snapshot, NoSpace, encode_record, plan, EXTENT


def fixture(path):
    for _, name, _, source in SOURCES:
        if source:
            target = path / source
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"KAT": name}))
    image, report = build(path)
    image = bytearray(image)
    first_free = 255 - report["free_segments"]
    size = struct.unpack_from("<I", image, 16)[0]
    payload = bytearray(image[32:32 + size])
    live_additions = [
        (2, 1, [(1, 0, 1, b"stay-1"), (1, 0, 2, b"obsolete-2"), (1, 0, 9, b"reset-later")]),
        (first_free, 2, [(1, 0, 3, b"stay-3"), (1, 0, 4, b"obsolete-4")]),
        (first_free + 1, 3, [(1, 0, 2, b"keep-2"), (1, 0, 4, b"keep-4"),
                             (2, 1, 5, b""), (2, 0, 6, b"base-override"), (1, 2, 9, b"")]),
    ]
    sequence = 0
    checkpoint = []
    for segment, epoch, records in live_additions:
        extent = write_journal_header(image, segment, epoch)
        if epoch != 1:
            payload.extend(extent)
        offset = segment * SEGMENT + 256
        for identity, op, key, text in records:
            sequence += 1
            edit = Edit(offset, identity, op, (key,), text)
            record = encode_record(edit, sequence)
            image[offset:offset + len(record)] = record
            if epoch == 1:
                checkpoint.append(edit)
            offset += edit.span
    for index, records in enumerate((checkpoint, sorted(checkpoint, key=lambda edit: (edit.text, edit.key)))):
        table = b"".join(struct.pack("<I", edit.address) for edit in records)
        segment = first_free + 2 + index
        image[segment * SEGMENT:segment * SEGMENT + len(table)] = table
        payload.extend(EXTENT.pack(segment, 1, len(table), zlib.crc32(table), 3 + index, 0))
    count = struct.unpack_from("<H", payload, 6)[0]
    struct.pack_into("<HQ", payload, 6, count + 4, len(checkpoint))
    for slot in range(2):
        header = catalog_header(slot + 1, payload)
        image[slot * SEGMENT:slot * SEGMENT + 32 + len(payload)] = header + payload
    snapshot = Snapshot(lambda at, n: bytes(image[at:at+n]), SEGMENT + 32, len(payload), zlib.crc32(payload))
    protected = {0, 1, 255}
    for start, count, *_ in snapshot.extents:
        protected.update(range(start, start + count))
    return image, snapshot, protected


def write_plan(path, update):
    result = bytearray(struct.pack("<4I", len(update.segments), update.metadata,
                                   len(update.payload), zlib.crc32(update.payload)))
    for segment, block in update.segments.items():
        result.extend(struct.pack("<2I", segment, len(block)))
        result.extend(block)
    path.write_bytes(result)


def main():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        image, snapshot, protected = fixture(path)
        import_plan = plan(snapshot, protected, {(1,): b"changed", (7, 8): "ça".encode()})
        clean_plan = plan(snapshot, protected)
        assert import_plan.changes == 2
        assert clean_plan.reclaimed == 2 and clean_plan.changes == 2
        assert plan(snapshot, protected, {(1,): b"stay-1"}) is None
        try:
            plan(snapshot, set(range(250)), {(11,): b"no space"})
        except NoSpace:
            pass
        else:
            raise AssertionError("unsafe staging allocation")
        for update in (import_plan, clean_plan):
            assert protected.isdisjoint(update.segments)
            assert all(2 <= index < 255 for index in update.segments)
        before = path / "before.bin"
        before.write_bytes(image)
        add, clean = path / "import.bin", path / "compact.bin"
        write_plan(add, import_plan)
        write_plan(clean, clean_plan)
        invalid_snapshot = copy(snapshot)
        invalid_snapshot.entries = snapshot.entries.copy()
        del invalid_snapshot.entries[(2, (6,))]
        invalid = path / "invalid.bin"
        write_plan(invalid, plan(invalid_snapshot, protected, {(11,): b"unsafe"}))
        binary = path / "test"
        subprocess.run([*compiler(), "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-I", str(ROOT / "tools/storage"), str(ROOT / "tools/tests/test_native_stage.cc"),
                        "-o", str(binary)], check=True)
        subprocess.run([str(binary), str(before), str(add), str(clean), str(invalid)], check=True)


if __name__ == "__main__":
    main()
