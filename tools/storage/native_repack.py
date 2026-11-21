#!/usr/bin/env python3
"""Offline-only repacking planner for a verified StenoSquid recovery bundle.

Emit a verified version 2 recovery bundle and a layout report. USB deployment
requires explicit approval and a matching guarded live pool or a spare.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import zlib

from build_native_image import LIMIT, SEGMENT, XIP, catalog_header, overlay_record_span, seed_entries, write_journal_header
from compile_dict import parse_outline
from hybrid import compact_entries
from native_layout import compact_member_sizes
from native_recovery import capture, digest, reconstruct, recover_usb, save_atomic, sync_directory
from native_snapshot import CompactImage, compile_snapshot, inspect_free, plan_change
from native_update import EXTENT, NoSpace, Snapshot, effective_entries, row_name

RESERVE = 3


def segment_count(size):
    return (size + SEGMENT - 1) // SEGMENT


def relocate_compact(blob, old_base, new_base, pointers=None):
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
    if pointers is not None:
        pointers.extend(sorted(seen))
    return bytes(output)


def verify_hints(blob, base):
    """Walk the physical SHI1 directory and compare its one-stroke results to JSC4."""
    start_address = base - XIP

    def read(address, size):
        offset = address - start_address
        return blob[offset:offset + size]

    compact_member_sizes(read, start_address, len(blob))
    _, members = struct.unpack_from("<II", blob, len(blob) - 8)
    directory = len(blob) - 12 - 8 * members
    text_bytes, text_pointer = struct.unpack_from("<II", blob, 8)
    text = text_pointer - base
    previous_end = (text + text_bytes + 7) & ~3
    for member in range(members):
        start, count = struct.unpack_from("<II", blob, directory + member * 8)
        if start != previous_end or start + count * 8 > directory:
            raise ValueError("Invalid compact hint record boundaries")
        previous_end = start + count * 8
        wanted = {}
        for outline, translation in compact_entries(blob, base, member, members):
            if "/" in outline:
                continue
            stroke, = parse_outline(outline)
            existing = wanted.get(translation)
            if stroke and (existing is None or (stroke.bit_count(), stroke) < (existing.bit_count(), existing)):
                wanted[translation] = stroke
        actual, last_offset = {}, -1
        for offset, stroke in struct.iter_unpack("<II", blob[start:previous_end]):
            if not last_offset < offset < text_bytes:
                raise ValueError("Invalid or unsorted compact hint text pointer")
            last_offset = offset
            end = blob.index(0, text + offset, text + text_bytes)
            translation = blob[text + offset:end].decode()
            if translation in actual:
                raise ValueError("Duplicate compact hint")
            actual[translation] = stroke
        if actual != wanted:
            raise ValueError("Compact one-stroke hints differ from their forward dictionary")
    if previous_end != directory:
        raise ValueError("Unexpected data before the compact hint directory")


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
    extents = []
    remap, new_bases = {}, {}
    next_segment = 2

    def place(data):
        nonlocal next_segment
        size = len(data)
        count = segment_count(size)
        position = next_segment * SEGMENT
        image[position:position + size] = data
        extents.append(EXTENT.pack(next_segment, count, size, zlib.crc32(data), 1, 0))
        next_segment += count
        return len(extents) - 1

    # Put the factory packages in their factory order even after an update
    # has moved their extents or the user has reordered their dictionaries.
    factory = {}
    for row in source.rows:
        identity, _, base = struct.unpack_from("<IIH", row)
        if 2 <= identity <= 7 and base != 65535:
            factory[base] = min(identity, factory.get(base, identity))
    compact_extents = sorted(
        (index for index, extent in enumerate(source.extents) if extent[4] == 1),
        key=lambda index: (factory.get(index, 8), index),
    )
    journal_segment = None
    for index in compact_extents:
        if index not in factory and journal_segment is None:
            journal_segment = next_segment
            extents.append(write_journal_header(image, journal_segment, 1))
            next_segment += 1
        start, _, size, _, _, _ = source.extents[index]
        address = start * SEGMENT
        new_address = next_segment * SEGMENT
        blob = source_image[address:address + size]
        verify_hints(blob, XIP + address)
        rebased = relocate_compact(blob, XIP + address, XIP + new_address)
        if relocate_compact(rebased, XIP + new_address, XIP + address) != blob:
            raise ValueError("Compact relocation changed bytes outside pointer fields")
        remap[index] = place(rebased)
    if journal_segment is None:
        journal_segment = next_segment
        extents.append(write_journal_header(image, journal_segment, 1))
        next_segment += 1
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
    end = seed_entries(image, extents, journal_segment, next_segment, after)
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
    per_dictionary = {}
    for row in source.rows:
        name = row_name(row)
        old = effective_entries(source, name, lambda address, n: source_image[address:address + n])
        new = effective_entries(target, name, lambda address, n: target_image[address:address + n])
        if old != new:
            raise ValueError(f"Packed target changed effective translations in {name}")
        per_dictionary[name] = dict(entries=len(new), export_sha256=hashlib.sha256(
            json.dumps(sorted(new.items()), ensure_ascii=False, separators=(",", ":")).encode()).hexdigest())
        if row[4]:
            for outline, text in old.items():
                source_winners.setdefault(outline, (name, text))
            for outline, text in new.items():
                target_winners.setdefault(outline, (name, text))
    if source_winners != target_winners:
        raise ValueError("Packed target changed effective priority lookup")
    return dict(per_dictionary_entries=sum(row["entries"] for row in per_dictionary.values()),
                dictionaries=per_dictionary, active_lookup_winners=len(target_winners),
                retained_overlay_records=len(expected_operations))


def verify_relocated(source_image, source, target_image, target, converted):
    """Existing baselines and SHI1 survive verbatim after normalizing pointers."""
    checked = set()
    for before, after in zip(source.rows, target.rows, strict=True):
        identity, _, old_base = struct.unpack_from("<IIH", before)
        new_base = struct.unpack_from("<H", after, 8)[0]
        if new_base == 65535:
            if old_base != 65535:
                raise ValueError("Packed target dropped a compact baseline")
            continue
        if (old_base == 65535) != (identity in converted):
            raise ValueError("Unexpected baseline conversion")
        start, _, size, _, kind, _ = target.extents[new_base]
        if kind != 1:
            raise ValueError("Packed target is missing a compact extent")
        address = start * SEGMENT
        blob = target_image[address:address + size]
        if new_base not in checked:
            verify_hints(blob, XIP + address)
            checked.add(new_base)
        if old_base != 65535:
            old_start, _, old_size, _, _, _ = source.extents[old_base]
            old_address = old_start * SEGMENT
            if relocate_compact(blob, XIP + address, XIP + old_address) != source_image[old_address:old_address + old_size]:
                raise ValueError("Packed target changed an existing compact baseline")


def consolidation_geometry(image, snapshot):
    """Run the production allocator on each installed user snapshot at its current size."""
    protected = {0, 1, 255}
    for start, span, *_ in snapshot.extents:
        protected.update(range(start, start + span))
    result = {}
    for row in snapshot.rows:
        if not row[11]:
            continue
        identity, _, base = struct.unpack_from("<IIH", row)
        start, span, size, *_ = snapshot.extents[base]
        address = start * SEGMENT
        pointers = []
        data = relocate_compact(image[address:address + size], XIP + address, 0, pointers)
        name = row_name(row)
        try:
            update = plan_change(snapshot, protected, identity, CompactImage(data, pointers), consolidate=True)
        except NoSpace as error:
            result[name] = dict(fits=False, image_segments=span, reason=str(error))
        else:
            result[name] = dict(fits=True, image_segments=span, staging_segments=len(update.segments))
    return result


def validate_pair(source_backup, target_backup, original, repacked):
    source_image, source = original
    target_image, target = repacked
    metadata = target_backup.get("repack")
    image_hash = hashlib.sha256(target_image).hexdigest()
    source_hash = source_backup["sha256"]
    if (not isinstance(metadata, dict) or metadata.get("source_bundle_sha256") != source_hash
            or metadata.get("target_image_sha256") != image_hash
            or source_backup["host_layout"] != target_backup["host_layout"]):
        raise ValueError("Optimized bundle does not match the source, target image or host layout")
    names = metadata.get("converted")
    if not isinstance(names, list) or any(not isinstance(name, str) for name in names) or len(names) != len(set(names)):
        raise ValueError("Invalid optimized bundle conversion list")
    candidates = {row_name(row): struct.unpack_from("<I", row)[0] for row in source.rows if row[8:10] == b"\xff\xff"}
    if set(names) - candidates.keys():
        raise ValueError("Optimized bundle converted an ineligible dictionary")
    converted = {candidates[name] for name in names}
    verify_relocated(source_image, source, target_image, target, converted)
    verification = verify_effective(source_image, source, target_image, target,
                                    operations_after(overlay_operations(source), converted), converted)
    before, after = layout_stats(source), layout_stats(target)
    if after["free_segments"] < RESERVE:
        raise ValueError("Optimized bundle does not retain the maintenance reserve")
    saved = before["used_segments"] - after["used_segments"]
    gained = after["largest_free_run"] - before["largest_free_run"]
    if saved > 0:
        recommendation = "space-savings"
    elif gained > 0 and saved == 0:
        recommendation = "contiguous-space-only"
    else:
        recommendation = "no-operation"
    return dict(format="stenosquid-offline-repack-1", source_bundle_sha256=source_hash,
                target_bundle_sha256=target_backup["sha256"], target_image_sha256=image_hash,
                source_capture=source_backup.get("capture"), source_journal_highwater=source.highwater,
                host_layout=source_backup["host_layout"], maintenance_reserve=RESERVE,
                source_layout=before, target_layout=after, saved_segments=saved,
                source_layout_scope="selected root in backup; excludes extents held only by the alternate root",
                largest_free_run_gain=gained, recommendation=recommendation, converted=names,
                target_same_size_consolidation=consolidation_geometry(target_image, target),
                verification=verification)


def build_repack(backup, names=None):
    source_image, source = reconstruct(backup)
    operations = overlay_operations(source)
    compact_segments = sum(segment_count(size) for _, _, size, _, kind, _ in source.extents if kind == 1)
    converted, refused, expected = choose_conversions(source, operations, compact_segments, names)
    target_image, target = pack_layout(source_image, source, operations, converted, expected)
    bundle = capture(lambda address, n: target_image[address:address + n], target, backup["host_layout"])
    names = [row_name(row) for row in source.rows if struct.unpack_from("<I", row)[0] in converted]
    bundle["repack"] = dict(source_bundle_sha256=backup["sha256"],
                            target_image_sha256=hashlib.sha256(target_image).hexdigest(), converted=names)
    bundle["sha256"] = digest(bundle)
    report = validate_pair(backup, bundle, (source_image, source), (target_image, target))
    return target_image, bundle, {**report, "refused": refused}


def repack(backup, names=None):
    image, _, report = build_repack(backup, names)
    return image, report


def read_bundle(path, *, durable=False):
    path = path.resolve(strict=True)
    with path.open("rb") as stream:
        if durable:
            os.fsync(stream.fileno())
            sync_directory(path.parent)
        raw = stream.read()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def check_live(console, bundle, *, target=False):
    """Compare every byte covered by the frozen backup. This grants no write permission."""
    from native_layout import read_layout
    from native_update import backup_all

    actual = backup_all(console)
    captured = bundle.get("capture", {})
    if not target and captured:
        for key in ("serial", "generation"):
            if captured.get(key) != actual.get("capture", {}).get(key):
                raise RuntimeError("Source USB serial or catalog generation changed. Capture a fresh backup and repack.")
    if any(actual[key] != bundle[key] for key in ("host_layout", "catalog", "extents")):
        raise RuntimeError("Live pool differs from the saved bundle, including journal or layout. "
                           "For repacking, capture a fresh source backup.")
    report = {"referenced_bytes_match": True, "host_layout": bundle["host_layout"]}
    if target:
        image, view = reconstruct(bundle)
        if console.command(f"native_install match RECOVERY {zlib.crc32(image)}", timeout=180) != "OK MATCH":
            raise RuntimeError("Installed full-pool CRC differs from the repacked recovery image.")
        layout = read_layout(console)
        report.update(full_pool_crc_match=True, free_segments=layout["free_segments"],
                      largest_free_run=layout["largest_free_run"])
        expected = layout_stats(view)
        if (layout["states"] != ["v", "v"] or report["free_segments"] != expected["free_segments"]
                or report["largest_free_run"] != expected["largest_free_run"]):
            raise RuntimeError("Installed allocator geometry differs from the optimized plan.")
    return report


def restore_repack(source_path, target_path, serial, confirmation, progress=print, *, live=False):
    if source_path.samefile(target_path):
        raise ValueError("Provide the original and optimized bundles as separate files.")
    source, source_file_hash = read_bundle(source_path, durable=True)
    target, file_hash = read_bundle(target_path, durable=True)
    if not serial or confirmation != f"{serial}:{file_hash}":
        raise ValueError("Confirm erase with the exact USB serial and optimized file SHA-256.")
    original = reconstruct(source)
    validate_pair(source, target, original, reconstruct(target))
    captured_serial = source.get("capture", {}).get("serial")
    if live and captured_serial != serial:
        raise ValueError("Live repack requires a fresh backup from the selected keyboard.")
    if not live and captured_serial == serial:
        raise ValueError("Use guarded live repack for the capture device.")
    progress(f"Confirmed device {serial}. Original file SHA-256 {source_file_hash}. "
             f"Optimized file SHA-256 {file_hash}. Recovery erases the entire external dictionary pool.")
    if live:
        recover_usb(target, serial, serial, progress, live_source=(source, original[1]))
    else:
        recover_usb(target, serial, serial, progress, repack_spare=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path, help="Original complete backup")
    parser.add_argument("--report", type=Path, help="Write an offline layout/parity report")
    parser.add_argument("--bundle-out", type=Path, help="Write a verified optimized version 2 bundle")
    parser.add_argument("--image", type=Path, help="Write an offline raw simulation image")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify-target", type=Path, help="Verify an optimized bundle against the original offline")
    mode.add_argument("--check-live-source", action="store_true", help="Read-only check against a live source")
    mode.add_argument("--check-live-target", type=Path, help="Verify a restored optimized spare after cold boot")
    mode.add_argument("--restore-spare", type=Path, help="Erase and recover an unprovisioned expendable spare")
    mode.add_argument("--restore-live", type=Path, help="Replace the captured live pool with guarded erase")
    parser.add_argument("--serial", help="Exact physical USB serial")
    parser.add_argument("--confirm-erase", metavar="SERIAL:SHA256", help="Repeat the spare serial and optimized file hash")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--convert", action="append", metavar="NAME", help="Consider only named candidates")
    selection.add_argument("--keep-representations", action="store_true", help="Pack without conversion")
    args = parser.parse_args()
    restore = args.restore_live or args.restore_spare
    comparing = args.verify_target or args.check_live_source or args.check_live_target or restore
    live = args.check_live_source or args.check_live_target or restore
    if bool(live) != bool(args.serial) or bool(restore) != bool(args.confirm_erase):
        parser.error("USB modes require --serial. Restore modes require --confirm-erase.")
    if comparing and (args.bundle_out or args.image or args.report or args.convert or args.keep_representations):
        parser.error("Planning options require offline repack mode.")
    if not comparing and not args.report:
        parser.error("Offline repack requires --report.")
    if restore:
        restore_repack(args.bundle, restore, args.serial, args.confirm_erase, live=bool(args.restore_live))
        return

    backup, source_file_hash = read_bundle(args.bundle)
    if comparing:
        target_path = args.verify_target or args.check_live_target
        if target_path:
            target, target_file_hash = read_bundle(target_path)
            report = validate_pair(backup, target, reconstruct(backup), reconstruct(target))
            report.update(source_file_sha256=source_file_hash, target_file_sha256=target_file_hash)
        if live:
            from native_install import open_console

            if not target_path:
                reconstruct(backup)
            with open_console(args.serial) as console:
                if console.serial != args.serial:
                    raise RuntimeError("The observed USB serial differs from the selected keyboard.")
                result = check_live(console, target if target_path else backup, target=bool(target_path))
                if not target_path:
                    from native_layout import read_layout

                    layout = read_layout(console)
                    result.update(free_segments_including_retained_root=layout["free_segments"],
                                  largest_free_run_including_retained_root=layout["largest_free_run"])
            print(json.dumps({**result, "serial": args.serial}, indent=2))
        else:
            print(json.dumps(report, indent=2))
        return

    paths = [path.resolve() for path in (args.report, args.image, args.bundle_out) if path]
    if len(set(paths)) != len(paths) or any(path.exists() for path in paths):
        parser.error("Output paths must be distinct new files")
    image, bundle, report = build_repack(backup, [] if args.keep_representations else args.convert)
    report["source_file_sha256"] = source_file_hash
    if args.bundle_out:
        save_atomic(args.bundle_out, bundle)
        reopened, file_hash = read_bundle(args.bundle_out)
        if reopened != bundle:
            raise RuntimeError("Saved optimized bundle differs from the verified plan.")
        report["target_file_sha256"] = file_hash
    if args.image:
        with args.image.open("xb") as stream:
            stream.write(image)
    save_atomic(args.report, report)
    print(f"Offline target verified. {report['saved_segments']} segments saved; "
          f"largest free run {report['target_layout']['largest_free_run']} segments. "
          f"Recommendation: {report['recommendation']}.")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, OSError, RuntimeError, ValueError, struct.error) as error:
        raise SystemExit(f"Error: {error}") from error
