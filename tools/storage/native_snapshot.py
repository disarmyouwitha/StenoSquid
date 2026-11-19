"""Preflight an experimental named snapshot. Pass --stage to publish it."""
import argparse
from dataclasses import dataclass
from pathlib import Path
import struct
import sys
import zlib

from build_native_image import SEGMENT, XIP
from compile_dict import DictionaryCollectionBuilder
from hybrid import format_stroke
from native_update import (
    EXTENT, NoSpace, Update, abort, publish, read_snapshot, row_name,
    update_command, validate_name,
)
from verify_mapped_dict import verify

SNAPSHOT_MARKER = 1


@dataclass
class CompactImage:
    data: bytes
    pointers: list

    def at(self, address):
        data = bytearray(self.data)
        for offset in self.pointers:
            pointer = struct.unpack_from("<I", data, offset)[0]
            if pointer >= len(data):
                raise ValueError("Compiler pointer outside the snapshot")
            struct.pack_into("<I", data, offset, address + pointer)
        return data


def compile_snapshot(name, entries):
    if not entries:
        raise ValueError("Use a journal for an empty dictionary")
    if any(not 1 <= len(key) <= 16 or any(not 0 < stroke < 1 << 24 for stroke in key) for key in entries):
        raise ValueError("This outline cannot be losslessly represented by a 24-bit JSC4 map")
    if any(len(value) > 2047 or b"\0" in value for value in entries.values()):
        raise ValueError("Translation outside the Native limits")
    source = {"/".join(map(format_stroke, strokes)): text.decode("utf-8") for strokes, text in entries.items()}
    if len(source) != len(entries):
        raise ValueError("Two outlines render to the same JSC4 key")
    builder = DictionaryCollectionBuilder(0)
    image = builder.build([(name, source)])
    verify(source, image, 0)
    return CompactImage(image, builder.pointer_offsets)


def plan_named_snapshot(snapshot, protected, name, compiled, limit=16):
    validate_name(name)
    if len(snapshot.rows) >= limit or snapshot.next_id == 0xffffffff:
        raise ValueError("The keyboard has reached its dictionary limit")
    if name in {row_name(row) for row in snapshot.rows}:
        raise ValueError("Dictionary name already exists")
    count = (len(compiled.data) + SEGMENT - 1) // SEGMENT
    free = set(range(2, 255)) - set(protected)
    if len(free) < count + 4 or len(snapshot.extents) == 253:
        raise NoSpace("No staging headroom for the snapshot and metadata")
    for segment in sorted(free):
        run = set(range(segment, segment + count))
        if run <= free:
            break
    else:
        raise NoSpace("No contiguous staging run for the snapshot")
    data = compiled.at(XIP + segment * SEGMENT)
    row = bytearray(96)
    struct.pack_into("<IIHBB", row, 0, snapshot.next_id, 1, len(snapshot.extents), 0, SNAPSHOT_MARKER)
    label = name.encode("ascii")
    row[12] = len(label)
    row[16:16 + len(label)] = label
    prefix = bytearray(snapshot.payload[:32])
    struct.pack_into("<IHH", prefix, 0, snapshot.next_id + 1, len(snapshot.rows) + 1, len(snapshot.extents) + 1)
    rows = snapshot.rows.copy()
    personal = next(i for i, old in enumerate(rows) if row_name(old) == "user_dictionary")
    rows.insert(personal + 1, row)
    extents = snapshot.payload[32 + 96 * len(snapshot.rows):]
    extent = EXTENT.pack(segment, count, len(data), zlib.crc32(data), 1, 0)
    payload = bytes(prefix) + b"".join(rows) + extents + extent
    blocks = {segment + i: data[i * SEGMENT:(i + 1) * SEGMENT] for i in range(count)}
    metadata = min(free - run)
    blocks[metadata] = payload
    return Update(blocks, metadata, payload, 0, 0)


def inspect_free(protected):
    free = set(range(2, 255)) - set(protected)
    largest = run = 0
    for segment in range(2, 255):
        run = run + 1 if segment in free else 0
        largest = max(largest, run)
    return len(free), largest


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dictionaries"))
    from import_personal import open_console, parse_named
    from native_install import wait_ready

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dictionary", type=Path, metavar="PLOVER.JSON")
    parser.add_argument("--name", required=True)
    parser.add_argument("--serial", required=True, help="USB serial of the experimental left half")
    parser.add_argument("--stage", action="store_true", help="Write free segments and publish a new catalog")
    args = parser.parse_args()
    validate_name(args.name)
    entries, _ = parse_named(args.dictionary.read_bytes())
    compiled = compile_snapshot(args.name, entries)
    required = (len(compiled.data) + SEGMENT - 1) // SEGMENT
    print(f"{len(entries):,} entries, {len(compiled.data):,} bytes, {required} contiguous segments")
    with open_console(args.serial) as console:
        if update_command(console, "snapshot-capabilities") != "OK SNAPSHOT1":
            raise RuntimeError("Flash the matching experimental firmware before testing snapshots")
        limit = int(update_command(console, "named-capabilities").split()[-1])
        answer = update_command(console, "begin snapshot-named" if args.stage else "begin backup")
        published = False
        try:
            snapshot, protected = read_snapshot(console, answer, metadata_only=True)
            free, largest = inspect_free(protected)
            print(f"{free} free segments, {largest} contiguous, 3 reserved for maintenance")
            update = plan_named_snapshot(snapshot, protected, args.name, compiled, limit=limit)
            print(f"Can stage safely: {len(update.segments)} segments including new catalog metadata")
            if args.stage:
                publish(console, update, print)
                published = True
        finally:
            if not published:
                abort(console)
    if args.stage:
        wait_ready(args.serial)


if __name__ == "__main__":
    main()
