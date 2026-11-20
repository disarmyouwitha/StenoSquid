"""Preflight an experimental named snapshot. Pass --stage to publish it."""
import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import struct
import sys
import zlib

from build_native_image import SEGMENT, XIP
from compile_dict import DictionaryCollectionBuilder, parse_outline
from hybrid import format_stroke
from native_update import (
    EXTENT, NoSpace, Update, abort, effective_entries, publish, read_flash, read_snapshot, row_name,
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
    """Compile a relocatable JSC4 baseline with the existing SHI1 hint table."""
    if not entries:
        raise ValueError("Use a journal for an empty dictionary")
    if any(not 1 <= len(key) <= 16 or any(not 0 < stroke < 1 << 24 for stroke in key) for key in entries):
        raise ValueError("This outline cannot be losslessly represented by a 24-bit JSC4 map")
    if any(len(value) > 2047 or b"\0" in value for value in entries.values()):
        raise ValueError("Translation outside the Native limits")
    source = {"/".join(map(format_stroke, strokes)): text.decode("utf-8") for strokes, text in entries.items()}
    if len(source) != len(entries):
        raise ValueError("Two outlines render to the same JSC4 key")
    builder = DictionaryCollectionBuilder(0, max_translation_bytes=2047)
    image = builder.build([(name, source)])
    verify(source, image, 0)
    return CompactImage(image, builder.pointer_offsets)


def stage_allocation(protected):
    free = set(range(2, 255)) - set(protected)
    blocks = {}
    extents = []

    def write(contents, kind):
        size = len(contents.data) if isinstance(contents, CompactImage) else len(contents)
        count = (size + SEGMENT - 1) // SEGMENT
        if len(free) < count + 3:
            raise NoSpace("Too little free space after protecting the three-segment reserve")
        for segment in sorted(free):
            run = set(range(segment, segment + count))
            if run <= free:
                break
        else:
            raise NoSpace("Not enough contiguous staging space")
        data = contents.at(XIP + segment * SEGMENT) if isinstance(contents, CompactImage) else contents
        blocks.update({segment + i: data[i * SEGMENT:(i + 1) * SEGMENT] for i in range(count)})
        free.difference_update(run)
        if kind:
            extents.append(EXTENT.pack(segment, count, size, zlib.crc32(data), kind, 0))
        return segment

    return blocks, extents, write


def plan_named_snapshot(snapshot, protected, name, compiled, limit=16):
    validate_name(name)
    if len(snapshot.rows) >= limit or snapshot.next_id == 0xffffffff:
        raise ValueError("The keyboard has reached its dictionary limit")
    if name in {row_name(row) for row in snapshot.rows}:
        raise ValueError("Dictionary name already exists")
    if len(snapshot.extents) == 253:
        raise NoSpace("Catalog extent limit")
    blocks, extents, write = stage_allocation(protected)
    write(compiled, 1)
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
    original_extents = snapshot.payload[32 + 96 * len(snapshot.rows):]
    payload = bytes(prefix) + b"".join(rows) + original_extents + b"".join(extents)
    metadata = write(payload, 0)
    return Update(blocks, metadata, payload, 0, 0)


def plan_change(snapshot, protected, identity, compiled=None, *, consolidate=False, convert=False):
    deleting = compiled is None
    if not deleting and not (consolidate or convert):
        raise ValueError("Existing compact dictionaries can only be rebuilt by consolidation.")
    target = next((row for row in snapshot.rows if struct.unpack_from("<I", row)[0] == identity), None)
    if target is None or (convert and (target[11] or row_name(target) != "user_dictionary" or deleting)):
        raise ValueError("Select a journal-only personal dictionary")
    if not convert and (target[11] != SNAPSHOT_MARKER or (deleting and row_name(target) == "user_dictionary")):
        raise ValueError("Select an eligible compact user dictionary")
    consolidate |= convert
    old_base = struct.unpack_from("<H", target, 8)[0]
    kept, remap = [], {}
    for i, extent in enumerate(snapshot.extents):
        if i != old_base and (not consolidate or extent[4] < 3):
            remap[i] = len(kept)
            kept.append(EXTENT.pack(*extent))
    blocks, added, write = stage_allocation(protected)
    if not deleting:
        remap[old_base] = len(kept)
        write(compiled, 1)
    if consolidate:
        retained = [entry for entry in snapshot.entries.values()
                    if entry.identity != identity or (convert and entry.operation == 1)]
        for kind, entries in ((3, sorted(retained, key=lambda entry: entry.key)),
                              (4, sorted((entry for entry in retained if not entry.operation),
                                         key=lambda entry: (entry.text, entry.key)))):
            if entries:
                write(b"".join(struct.pack("<I", entry.address) for entry in entries), kind)
    extents = kept + added
    if len(extents) > 253:
        raise NoSpace("Catalog extent limit")
    rows = []
    for original in snapshot.rows:
        if deleting and original is target:
            continue
        row = bytearray(original)
        base = struct.unpack_from("<H", row, 8)[0]
        if original is target and convert:
            struct.pack_into("<HBB", row, 8, remap[old_base], 0, SNAPSHOT_MARKER)
        elif base != 65535:
            if base == old_base and original is not target:
                raise ValueError("Snapshot unexpectedly shares its compact extent")
            struct.pack_into("<H", row, 8, remap[base])
        rows.append(row)
    prefix = bytearray(snapshot.payload[:32])
    struct.pack_into("<HH", prefix, 4, len(rows), len(extents))
    if consolidate:
        struct.pack_into("<Q", prefix, 8, snapshot.highwater)
    payload = bytes(prefix) + b"".join(rows) + b"".join(extents)
    metadata = write(payload, 0)
    return Update(blocks, metadata, payload, 0, 0)


def personal_baseline(snapshot, additions=None):
    # Imported entries fill gaps. An existing local set or tombstone wins.
    effective = dict(additions or {})
    for entry in snapshot.entries.values():
        if entry.identity != snapshot.personal:
            continue
        if entry.operation:
            effective.pop(entry.strokes, None)
        else:
            effective[entry.strokes] = entry.text
    return effective


def change_snapshot(console, operation, name, *, source=None, stage=False, progress=print,
                    preview=False, expected=None, target_id=None):
    if operation not in {"personal", "consolidate", "delete"} or (
        (preview or expected is not None) and (operation == "delete" or (preview and stage))
    ):
        raise ValueError("Select an optimization to preview or stage.")
    if update_command(console, "snapshot-capabilities") != "OK SNAPSHOT2":
        raise RuntimeError("Flash the matching experimental lifecycle firmware")
    validate_name(name)
    converting = operation == "personal"
    if converting and update_command(console, "personal-snapshot-capabilities") != "OK PERSONAL1":
        raise RuntimeError("Flash the experimental personal conversion firmware")
    expected_kind = "personal" if converting else "snapshot"
    row = next((row for row in json.loads(console.command("native_catalog"))
                if row["kind"] == expected_kind and row["name"] == name), None)
    if row is None or (target_id is not None and row["id"] != target_id):
        raise ValueError("The selected dictionary is no longer eligible. Refresh and choose it again.")
    identity = row["id"]
    answer = update_command(console, f"begin snapshot-{operation} {identity}")
    published = False
    try:
        consolidating = operation == "consolidate"
        report = preview or expected is not None
        snapshot, protected = read_snapshot(console, answer, metadata_only=not (consolidating or converting or report))
        if not any(row_name(row) == name and struct.unpack_from("<I", row)[0] == identity for row in snapshot.rows):
            raise RuntimeError("Catalog changed before staging. Refresh and retry.")
        # Read generation under the staging lease. A textually identical plan
        # from a different generation or keyboard needs its own approval.
        generation = json.loads(console.command("user_dictionary_status"))["generation"] if report else None
        compiled = None
        if converting:
            compiled = compile_snapshot(name, personal_baseline(snapshot, source))
        elif consolidating:
            entries = effective_entries(snapshot, name, lambda at, n: read_flash(console, at, n))
            compiled = compile_snapshot(name, {parse_outline(outline): text.encode("utf-8")
                                               for outline, text in entries.items()})
        if compiled is not None:
            free, largest = inspect_free(protected)
            sectors = (len(compiled.data) + SEGMENT - 1) // SEGMENT
            progress(f"{len(compiled.data):,} compact bytes · {sectors} contiguous segments needed · "
                     f"{free} free, largest run {largest}, reserve 3")
        update = plan_change(snapshot, protected, identity, compiled, consolidate=consolidating, convert=converting)
        progress(f"{operation.capitalize()} {name}: {len(update.segments)} staging segments")
        if report:
            digest = hashlib.sha256()
            digest.update(json.dumps([console.serial, generation, operation, identity, snapshot.highwater,
                                      sorted(protected)], separators=(",", ":")).encode())
            digest.update(snapshot.payload)
            for segment, block in sorted(update.segments.items()):
                digest.update(struct.pack("<II", segment, len(block)))
                digest.update(block)
            token = digest.hexdigest()
            if expected is not None and expected != token:
                raise RuntimeError("The optimization plan changed. Preview it again before staging.")
            if preview:
                return dict(token=token, id=identity, name=name, operation=operation, generation=generation,
                            compact_bytes=len(compiled.data), image_segments=sectors,
                            staging_segments=len(update.segments), free_segments=free, largest_free_run=largest,
                            reserve=3)
        if stage:
            publish(console, update, progress)
            published = True
        return published
    finally:
        if not published:
            abort(console)


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
    parser.add_argument("dictionary", type=Path, metavar="PLOVER.JSON", nargs="?")
    parser.add_argument("--name")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--consolidate", action="store_true", help="Fold current overlays into a new baseline")
    action.add_argument("--delete", action="store_true", help="Retire the named snapshot")
    action.add_argument("--convert-personal", action="store_true", help="Convert in place with optional fill-only JSON")
    parser.add_argument("--serial", required=True, help="USB serial of the experimental left half")
    parser.add_argument("--format", choices=("auto", "journal", "snapshot"), default="auto",
                        help="Choose an eligible representation for a new named dictionary")
    parser.add_argument("--stage", action="store_true", help="Write free segments and publish a new catalog")
    args = parser.parse_args()
    if args.convert_personal:
        if args.name:
            parser.error("Personal conversion always targets user_dictionary")
        args.name = "user_dictionary"
    elif not args.name:
        parser.error("--name is required")
    validate_name(args.name)
    if not args.convert_personal and bool(args.dictionary) == bool(args.consolidate or args.delete):
        parser.error("Supply a Plover JSON file for a new dictionary only")
    entries = None
    if args.dictionary:
        entries, _ = parse_named(args.dictionary.read_bytes())
    if args.consolidate or args.delete or args.convert_personal:
        if args.format != "auto":
            parser.error("--format only applies to new named dictionaries")
        operation = "personal" if args.convert_personal else "consolidate" if args.consolidate else "delete"
        with open_console(args.serial) as console:
            published = change_snapshot(console, operation, args.name,
                                        source=entries, stage=args.stage)
        if published:
            wait_ready(args.serial)
        return

    from native_import_plan import named_import

    with open_console(args.serial) as console:
        report = named_import(console, args.name, entries, preference=args.format, stage=args.stage)
        print(json.dumps(report, indent=2))
    if args.stage:
        wait_ready(args.serial)


if __name__ == "__main__":
    main()
