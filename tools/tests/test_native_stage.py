#!/usr/bin/env python3
"""Exercise the actual planner, staged store and every torn-write boundary."""

from copy import copy
import hashlib
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
sys.path.insert(0, str(ROOT / "tools/dictionaries"))
from native_base_update import GROUPS, Image, plan_replacements
from compile_dict import DictionaryCollectionBuilder, parse_outline
from verify_mapped_dict import verify


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
        named = path / "named.bin"
        named_plan = plan(snapshot, protected, {(1,): b"named override"}, name="Medical")
        write_plan(named, named_plan)
        named_image = bytearray(image)
        for slot, block in named_plan.segments.items():
            at = slot * SEGMENT
            named_image[at:at + len(block)] = block
        for slot in range(2):
            at = slot * SEGMENT
            body = catalog_header(slot + 3, named_plan.payload) + named_plan.payload
            named_image[at:at + len(body)] = body
        named_snapshot = Snapshot(lambda at, n: named_image[at:at+n], SEGMENT + 32,
                                  len(named_plan.payload), zlib.crc32(named_plan.payload), metadata_only=True)
        named_protected = {0, 1, 255}
        for start, count, *_ in named_snapshot.extents:
            named_protected.update(range(start, start + count))
        named_before, rename, delete = (path / name for name in ("named-before.bin", "rename.bin", "delete.bin"))
        named_before.write_bytes(named_image)
        write_plan(rename, plan(named_snapshot, named_protected, rename=("Medical", "Medical Practice")))
        write_plan(delete, plan(named_snapshot, named_protected, delete="Medical"))
        assert plan(named_snapshot, named_protected, rename=("Medical", "Medical")) is None
        reorder = path / "reorder.bin"
        write_plan(reorder, plan(snapshot, protected, move=("Lapwing Base", 1)))
        for kwargs in ({"name": "Lapwing Base"}, {"name": "Too,long"}, {"name": "Medical", "limit": 7},
                       {"move": ("Missing", 1)}, {"move": ("Lapwing Base", 8)},
                       {"rename": ("user_dictionary", "Private")}, {"delete": "Lapwing Base"}):
            try:
                plan(snapshot, protected, {}, **kwargs)
            except ValueError:
                pass
            else:
                raise AssertionError(f"Invalid catalog operation accepted: {kwargs}")
        assert plan(snapshot, protected, move=("user_dictionary", 1)) is None
        group = GROUPS[0]
        definitions = [(name, {"KAT": "new upstream" if index == 0 else name})
                       for index, name in enumerate(group)]
        digests = {name: hashlib.sha256(json.dumps(entries).encode()).digest()
                   for name, entries in definitions}
        compiler_image = DictionaryCollectionBuilder(0)
        compiled = Image(definitions, compiler_image.build(definitions), compiler_image.pointer_offsets, digests)
        conflict_snapshot = copy(snapshot)
        conflict_snapshot.entries = snapshot.entries.copy()
        strokes = parse_outline("KAT")
        conflict = Edit(0, 2, 0, strokes, b"my override")
        conflict_snapshot.entries[conflict.key] = conflict
        revised, changed, conflicts = plan_replacements(conflict_snapshot, protected, {group: compiled},
                                                        lambda at, n: bytes(image[at:at+n]))
        assert changed == ["Lapwing Commands"]
        assert conflicts == [{"dictionary": "Lapwing Commands", "outline": "KAT",
                              "before": "Lapwing Commands", "after": "new upstream", "local": "my override"}]
        image_segment, binary_image = next(iter(revised.segments.items()))
        for index, (_, entries) in enumerate(definitions):
            verify(entries, binary_image, 0x12000000 + image_segment * SEGMENT, index, len(group))
        try:
            plan_replacements(snapshot, set(range(251)), {group: compiled})
        except NoSpace:
            pass
        else:
            raise AssertionError("Base update ignored the staging reserve")
        replace = path / "replace.bin"
        write_plan(replace, revised)
        binary = path / "test"
        subprocess.run([*compiler(), "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-I", str(ROOT / "tools/storage"), str(ROOT / "tools/tests/test_native_stage.cc"),
                        "-o", str(binary)], check=True)
        subprocess.run([str(binary), str(before), str(add), str(clean), str(invalid), str(named), str(reorder),
                        str(named_before), str(rename), str(delete), str(replace)], check=True)


if __name__ == "__main__":
    main()
