#!/usr/bin/env python3
"""Offline-only repacking planner for a verified StenoSquid recovery bundle.

Produces an optional raw simulation image and a layout report. No USB API,
install manifest, or recovery bundle is emitted by this command.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import zlib

from build_native_image import LIMIT, SEGMENT, XIP, catalog_header, overlay_record_span, seed_entries, write_journal_header
from native_recovery import capture, reconstruct, save_atomic
from native_snapshot import compile_snapshot, inspect_free
from native_update import EXTENT, Snapshot, effective_entries, row_name

RESERVE = 3


def segment_count(size):
    return (size + SEGMENT - 1) // SEGMENT


def relocate_compact(blob, old_base, new_base):
    """Adjust only absolute JSC4 fields emitted by the supported compiler."""
    if len(blob) < 40 or blob[:4] != b"JSC4" or any(blob[6:8]) or any(blob[16:32]):
        raise ValueError("Unsupported compact collection for offline relocation")
    count = struct.unpack_from("<H", blob, 4)[0]
    if not 1 <= count <= 64 or 36 + 4 * count > len(blob):
        raise ValueError("Invalid compact collection member count")
    output = bytearray(blob)
    seen = set()

    def pointer(field, needed=1, *, alignment=1):
        if field < 0 or field > len(blob) - 4:
            raise ValueError("Invalid compact pointer field")
        at = struct.unpack_from("<I", blob, field)[0] - old_base
        if at < 0 or at % alignment or needed > len(blob) - at:
            raise ValueError("Compact pointer outside its extent")
        if field not in seen:
            struct.pack_into("<I", output, field, new_base + at)
            seen.add(field)
        return at

    text_bytes = struct.unpack_from("<I", blob, 8)[0]
    text = pointer(12, text_bytes + 4)
    for member in range(count):
        definition = pointer(36 + member * 4, 16, alignment=4)
        maximum = blob[definition + 1]
        if not 1 <= maximum <= 16 or any(blob[definition + 2:definition + 4]):
            raise ValueError("Unsupported compact dictionary definition")
        pointer(definition + 4)
        if pointer(definition + 8, text_bytes) != text:
            raise ValueError("Unsupported per-dictionary text block")
        table = pointer(definition + 12, maximum * 12, alignment=4)
        for index in range(maximum):
            field = table + index * 12
            pointer(field + 4)
            pointer(field + 8, alignment=4)
    return bytes(output)


def overlay_geometry(entries):
    """Mirror the stock builder's sequential packing and its two indexes."""
    journals, consumed, sets = 1, 256, 0
    for (_, strokes), (operation, translation) in entries.items():
        span = overlay_record_span(strokes, translation)
        if consumed + span > SEGMENT:
            journals, consumed = journals + 1, 256
        consumed += span
        sets += operation == 0
    return journals, segment_count(4 * len(entries)), segment_count(4 * sets)


def layout_stats(view):
    owned = {0, 1, 255}
    regions = []
    kinds = {1: "compact", 2: "journal", 3: "forward-index", 4: "reverse-index"}
    for start, count, size, _, kind, _ in view.extents:
        owned.update(range(start, start + count))
        regions.append(dict(start=start, segments=count, bytes=size, kind=kinds[kind]))
    free, run = inspect_free(owned)
    return dict(used_segments=255 - free, free_segments=free, largest_free_run=run,
                free_after_reserve=max(0, free - RESERVE), regions=regions)


def overlay_operations(view):
    return {key: (record.operation, record.text) for key, record in sorted(view.entries.items())}


def operations_after(operations, converted):
    return {key: value for key, value in operations.items() if key[0] not in converted or value[0] != 0}


def choose_conversions(source, operations, compact_segments, names):
    current = compact_segments + sum(overlay_geometry(operations))
    converted, refusals = {}, {}
    eligible = {row_name(row): row for row in source.rows if row[8:10] == b"\xff\xff"}
    ordered = list(eligible) if names is None else list(dict.fromkeys(names))
    for name in ordered:
        row = eligible.get(name)
        if row is None:
            refusals[name] = "Only journal-only user dictionaries can be converted"
            continue
        identity = struct.unpack_from("<I", row)[0]
        entries = {strokes: text for (owner, strokes), (op, text) in operations.items()
                   if owner == identity and not op}
        if not entries:
            refusals[name] = "No live translations to compact"
            continue
        try:
            compiled = compile_snapshot(name, entries)
        except ValueError as error:
            refusals[name] = str(error)
            continue
        extra = segment_count(len(compiled.data))
        next_operations = operations_after(operations, {*converted, identity})
        prospective = compact_segments + extra + sum(overlay_geometry(next_operations))
        if prospective >= current:
            refusals[name] = "No packed segment savings after retaining tombstones and other overlays"
            continue
        current = prospective
        compact_segments += extra
        converted[identity] = compiled
    return converted, refusals, current


def pack_layout(source_image, source, operations, converted, expected_segments):
    if expected_segments + 2 > 255 - RESERVE:
        raise ValueError("The packed target cannot retain the three-segment maintenance reserve")
    image = bytearray(b"\xff" * LIMIT)
    extents = [write_journal_header(image, 2, 1)]
    remap, new_bases = {}, {}
    next_segment = 3

    def place(data):
        nonlocal next_segment
        size = len(data)
        count = segment_count(size)
        position = next_segment * SEGMENT
        image[position:position + size] = data
        extents.append(EXTENT.pack(next_segment, count, size, zlib.crc32(data), 1, 0))
        next_segment += count
        return len(extents) - 1

    for index, (start, _, size, _, kind, _) in enumerate(source.extents):
        if kind != 1:
            continue
        address = start * SEGMENT
        new_address = next_segment * SEGMENT
        blob = source_image[address:address + size]
        rebased = relocate_compact(blob, XIP + address, XIP + new_address)
        if relocate_compact(rebased, XIP + new_address, XIP + address) != blob:
            raise ValueError("Compact relocation changed bytes outside pointer fields")
        remap[index] = place(rebased)
    for identity, compiled in converted.items():
        new_bases[identity] = place(compiled.at(XIP + next_segment * SEGMENT))

    rows = []
    for original in source.rows:
        row = bytearray(original)
        identity, _, base = struct.unpack_from("<IIH", row)
        if identity in new_bases:
            struct.pack_into("<HBB", row, 8, new_bases[identity], 0, 1)
        elif base != 65535:
            struct.pack_into("<H", row, 8, remap[base])
        rows.append(row)
    after = operations_after(operations, converted)
    end = seed_entries(image, extents, next_segment, after)
    if end != 2 + expected_segments:
        raise ValueError("Packed estimate differs from the generated image")
    payload = struct.pack("<IHHQ16x", source.next_id, len(rows), len(extents), len(after))
    payload += b"".join(rows) + b"".join(extents)
    for root in range(2):
        address = root * SEGMENT
        image[address:address + 32 + len(payload)] = catalog_header(root + 1, payload) + payload
    data = bytes(image)
    view = Snapshot(lambda address, n: data[address:address + n], 32, len(payload),
                    zlib.crc32(payload), strict=True)
    return data, view


def verify_effective(source_image, source, target_image, target, expected_operations, converted):
    if source.next_id != target.next_id or source.personal != target.personal:
        raise ValueError("Packed target changed a catalog identity counter")
    if [row[:8] + row[12:] for row in source.rows] != [row[:8] + row[12:] for row in target.rows]:
        raise ValueError("Packed target changed priority, flags, names or pinned provenance")
    for before, after in zip(source.rows, target.rows, strict=True):
        identity = struct.unpack_from("<I", before)[0]
        if after[10:12] != (b"\x00\x01" if identity in converted else before[10:12]):
            raise ValueError("Packed target changed an unrelated dictionary representation")
    if overlay_operations(target) != expected_operations:
        raise ValueError("Packed target changed shared edits or tombstones")
    source_winners, target_winners = {}, {}
    entries = 0
    for row in source.rows:
        name = row_name(row)
        old = effective_entries(source, name, lambda address, n: source_image[address:address + n])
        new = effective_entries(target, name, lambda address, n: target_image[address:address + n])
        if old != new:
            raise ValueError(f"Packed target changed effective translations in {name}")
        entries += len(new)
        if row[4]:
            for outline, text in old.items():
                source_winners.setdefault(outline, (name, text))
            for outline, text in new.items():
                target_winners.setdefault(outline, (name, text))
    if source_winners != target_winners:
        raise ValueError("Packed target changed effective priority lookup")
    return dict(per_dictionary_entries=entries, active_lookup_winners=len(target_winners),
                retained_overlay_records=len(expected_operations))


def repack(backup, names=None):
    source_image, source = reconstruct(backup)
    operations = overlay_operations(source)
    compact_segments = sum(segment_count(size) for _, _, size, _, kind, _ in source.extents if kind == 1)
    converted, refused, expected = choose_conversions(source, operations, compact_segments, names)
    target_image, target = pack_layout(source_image, source, operations, converted, expected)
    # Use the bundle validator to check all compact members, search indexes,
    # restored journal references and full checksums in their new locations.
    capture(lambda address, n: target_image[address:address + n], target, backup["host_layout"])
    verification = verify_effective(source_image, source, target_image, target,
                                    operations_after(operations, converted), converted)
    before, after = layout_stats(source), layout_stats(target)
    report = dict(format="stenosquid-offline-repack-1", source_bundle_sha256=backup["sha256"],
                  target_image_sha256=hashlib.sha256(target_image).hexdigest(), host_layout=backup["host_layout"],
                  maintenance_reserve=RESERVE, source_layout=before, target_layout=after,
                  saved_segments=before["used_segments"] - after["used_segments"],
                  converted=[row_name(row) for row in source.rows if struct.unpack_from("<I", row)[0] in converted],
                  refused=refused, verification=verification)
    return target_image, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--image", type=Path, help="Write an offline raw simulation image")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--convert", action="append", metavar="NAME", help="Consider only named candidates")
    selection.add_argument("--keep-representations", action="store_true", help="Pack without conversion")
    args = parser.parse_args()
    if args.image and args.image.resolve() == args.report.resolve():
        parser.error("The report and image must have different paths")
    if args.report.exists() or (args.image and args.image.exists()):
        parser.error("Output paths must be new files")
    image, report = repack(json.loads(args.bundle.read_text(encoding="utf-8")),
                           [] if args.keep_representations else args.convert)
    if args.image:
        with args.image.open("xb") as stream:
            stream.write(image)
    save_atomic(args.report, report)
    print(f"Offline target verified. {report['saved_segments']} segments saved; "
          f"largest free run {report['target_layout']['largest_free_run']} segments.")


if __name__ == "__main__":
    main()
