#!/usr/bin/env python3
"""Generate (or check) golden vectors for the JavaScript snapshot/import planners (snapshot_plan.js, Manager milestone M2).

Oracle: Python's tools/storage/native_snapshot.py (plan_named_snapshot, plan_change, merge_rows, merged_entries,
journal_baseline, inspect_free, the allocator and the token digests) and native_import_plan.py (plan_compact_import,
consolidation_cost). Compiled images enter these planners as plain data (bytes plus pointer offsets), so a deterministic
synthetic image is used instead of the compiler; the compiler is gated separately.

    python3 tools/manager/browser/golden_snapshot_plan.py            # rewrite golden/snapshot_plan_vectors.json
    python3 tools/manager/browser/golden_snapshot_plan.py --check    # fail if the committed file is stale
"""
import hashlib
import json
from pathlib import Path
import struct
import sys
import zlib

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import golden_plan as plan_vectors
import golden_snapshot as base_vectors  # also puts the tools/ directories on sys.path
from build_native_image import LARGE_POOL_END, SEGMENT, SMALL_POOL_END, catalog_header
from native_import_plan import consolidation_cost, metadata, plan_compact_import
from native_snapshot import (CompactImage, inspect_free, journal_baseline, merge_rows, merged_entries, offline_merge_token,
                             personal_baseline, plan_change, plan_named_snapshot, stage_allocator)
from compile_dict import parse_outline
from native_update import NoSpace, Snapshot

OUT = HERE / "golden" / "snapshot_plan_vectors.json"


def compiled_spec(length, pointers, delta=None):
    """delta: None keeps every pointer word in range; otherwise the first pointer word is set to length + delta."""
    return {"length": length, "pointers": pointers, "pointerDelta": delta}


def build_compiled(spec):
    """The same construction as test_snapshot_plan.mjs: a patterned image with in-range pointer words."""
    length, pointers = spec["length"], spec["pointers"]
    data = bytearray((i * 7 + 3) % 251 for i in range(length))
    for offset in pointers:
        struct.pack_into("<I", data, offset, (offset * 13 + 8) % length)
    if spec["pointerDelta"] is not None:
        struct.pack_into("<I", data, pointers[0], length + spec["pointerDelta"])
    return CompactImage(bytes(data), list(pointers))


def serialise(update):
    if update is None:
        return None
    return {"metadata": update.metadata, "payload": update.payload.hex(),
            "segments": [[segment, len(block), hashlib.sha256(bytes(block)).hexdigest()] for segment, block in update.segments.items()]}


def outcome(function):
    try:
        return {"value": function()}
    except NoSpace as error:
        return {"error": str(error), "kind": "NoSpace"}
    except ValueError as error:
        return {"error": str(error), "kind": "ValueError"}


def apply_update(image, update, generation):
    """Commit an update the way the keyboard does for the purposes of the next planning step."""
    result = bytearray(image)
    for segment, block in update.segments.items():
        result[segment * SEGMENT:segment * SEGMENT + len(block)] = bytes(block)
    for slot in range(2):
        blob = catalog_header(generation + slot, update.payload) + update.payload
        result[slot * SEGMENT:slot * SEGMENT + len(blob)] = blob
    return result


def strokes_json(mapping):
    return [[list(strokes), text.hex()] for strokes, text in mapping.items()]


def generate():
    images, cases = {}, []
    small, large = base_vectors.fixture(SMALL_POOL_END), base_vectors.fixture(LARGE_POOL_END)
    one = compiled_spec(40000, [0, 8, 40, 96, 4096])
    two = compiled_spec(90000, [0, 12, 4000, 70000, 89996])
    chain = {"small": (small, SMALL_POOL_END), "large": (large, LARGE_POOL_END)}

    # Two chained imports give a catalog with adjacent compact (snapshot) rows, which change and merge operate on.
    image = small
    for number, (title, spec) in enumerate((("Snap One", one), ("Snap Two", compiled_spec(30000, [0, 16, 200]))), 1):
        snapshot = plan_vectors.snapshot_of(image, SMALL_POOL_END)
        update = plan_named_snapshot(snapshot, plan_vectors.owned(image, SMALL_POOL_END), title, build_compiled(spec), limit=16)
        image = apply_update(image, update, 10 * number)
    chain["snaps"] = (image, SMALL_POOL_END)

    for label, (data, pool_end) in chain.items():
        images[label] = plan_vectors.record_image(data, pool_end)

    def named(label, title, spec, *, limit=16, protected=None, name_image=None):
        data, pool_end = chain[name_image or label]
        snapshot = plan_vectors.snapshot_of(data, pool_end)
        owned = plan_vectors.owned(data, pool_end) if protected is None else protected
        record = {"kind": "named", "image": name_image or label, "name": title, "compiled": spec, "limit": limit, "protected": sorted(owned)}
        record["expect"] = outcome(lambda: serialise(plan_named_snapshot(snapshot, owned, title, build_compiled(spec), limit=limit)))
        cases.append(record)
        return record

    for label in ("small", "large"):
        pool_end = chain[label][1]
        everything = set(range(pool_end + 1))
        named(label, "New Compact", one)
        named(label, "Two Segment Image", two)
        named(label, "Custom A", one)                       # name already exists
        named(label, "bad name ", one)                      # invalid
        named(label, "Limit Hit", one, limit=len(plan_vectors.snapshot_of(*chain[label][:1], pool_end).rows))
        named(label, "Bad Pointer", compiled_spec(40000, [0, 8], delta=5))
        named(label, "Pointer At The End", compiled_spec(40000, [0, 8], delta=0))
        named(label, "Pointer Just Inside", compiled_spec(40000, [0, 8], delta=-1))
        named(label, "No Room", one, protected=everything - {pool_end - 1, pool_end - 2, pool_end - 3, pool_end - 4})
        named(label, "Exactly Fits", one, protected=everything - {pool_end - 5, pool_end - 4, pool_end - 3, pool_end - 2, pool_end - 1})
        named(label, "Image Needs Two", two, protected=everything - {pool_end - 5, pool_end - 4, pool_end - 3, pool_end - 2, pool_end - 1})
        named(label, "Nothing Protected", one, protected=set())

    # Catalog edits on the chained image: rows 'Snap Two' (upper) and 'Snap One' (lower) are adjacent compact dictionaries.
    data, pool_end = chain["snaps"]
    snapshot = plan_vectors.snapshot_of(data, pool_end)
    owned = plan_vectors.owned(data, pool_end)
    ids = {base_vectors_name: struct.unpack_from("<I", row)[0]
           for row in snapshot.rows for base_vectors_name in [row[16:16 + row[12]].decode()]}
    replacement = compiled_spec(25000, [0, 24, 512])

    def change(title, identity, spec=None, **flags):
        compiled = None if spec is None else build_compiled(spec)
        record = {"kind": "change", "image": "snaps", "title": title, "identity": identity, "compiled": spec, "flags": dict(flags),
                  "protected": sorted(owned)}
        kwargs = {key: value for key, value in flags.items()}
        record["expect"] = outcome(lambda: serialise(plan_change(snapshot, owned, identity, compiled, **kwargs)))
        cases.append(record)

    snap_one, snap_two, personal = ids["Snap One"], ids["Snap Two"], ids["user_dictionary"]
    change("rebuild a compact dictionary (consolidate)", snap_one, replacement, consolidate=True)
    change("compiled replacement without consolidate", snap_one, replacement)
    change("delete a compact dictionary", snap_one)
    change("delete the personal dictionary", personal)
    change("clear the personal dictionary", personal, clear=True)
    change("clear a non-personal dictionary", snap_one, clear=True)
    change("convert a journal-only dictionary", ids["Custom A"], replacement, convert=True)
    change("convert a factory dictionary", ids["Lapwing Base"], replacement, convert=True)
    change("convert a compact dictionary", snap_one, replacement, convert=True)
    change("select a missing dictionary", 123456, replacement, consolidate=True)
    change("merge adjacent compact dictionaries", snap_two, replacement, consolidate=True, merge_id=snap_one)
    change("merge in the wrong order", snap_one, replacement, consolidate=True, merge_id=snap_two)
    change("merge without a replacement", snap_two, None, consolidate=True, merge_id=snap_one)
    change("merge with convert", snap_two, replacement, consolidate=True, convert=True, merge_id=snap_one)
    change("merge the personal dictionary", personal, replacement, consolidate=True, merge_id=snap_one)
    everything = set(range(pool_end + 1))
    tight = everything - {pool_end - 1, pool_end - 2, pool_end - 3}
    record = {"kind": "change", "image": "snaps", "title": "rebuild with no room", "identity": snap_one, "compiled": replacement,
              "flags": {"consolidate": True}, "protected": sorted(tight)}
    record["expect"] = outcome(lambda: serialise(plan_change(snapshot, tight, snap_one, build_compiled(replacement), consolidate=True)))
    cases.append(record)

    for upper, lower in ((snap_two, snap_one), (snap_one, snap_two), (snap_two, personal), (personal, snap_two), (ids["Custom A"], ids["Custom B"])):
        record = {"kind": "mergeRows", "image": "snaps", "upper": upper, "lower": lower}
        def pair(u=upper, l=lower):
            a, b = merge_rows(snapshot, u, l)
            return [snapshot.rows.index(a), snapshot.rows.index(b)]
        record["expect"] = outcome(pair)
        cases.append(record)

    # Ordering helpers on journal entries.
    for title, identity_name, additions in (
            ("personal baseline with additions", "personal", {(0x10,): b"imported (loses)", (0x77,): b"imported only", (0x40,): b"after reset"}),
            ("personal baseline, no additions", "personal", None),
            ("named baseline", "Custom A", {(0x50,): b"x", (0x51,): b"y"})):
        smallish, small_end = chain["small"]
        snap = plan_vectors.snapshot_of(smallish, small_end)
        if identity_name == "personal":
            result = personal_baseline(snap, additions)
        else:
            custom = next(struct.unpack_from("<I", r)[0] for r in snap.rows if r[16:16 + r[12]].decode() == identity_name)
            result = journal_baseline(snap, custom, additions)
        record = {"kind": "baseline", "image": "small", "title": title, "who": identity_name,
                  "additions": None if additions is None else strokes_json(additions), "expect": strokes_json(result)}
        cases.append(record)

    cases.append({"kind": "inspectFree", "poolEnd": SMALL_POOL_END, "protected": sorted(plan_vectors.owned(small, SMALL_POOL_END)),
                  "expect": list(inspect_free(plan_vectors.owned(small, SMALL_POOL_END), SMALL_POOL_END))})
    for protected in (set(), set(range(0, 255, 2)), set(range(10, 255)) | {40}):
        cases.append({"kind": "inspectFree", "poolEnd": SMALL_POOL_END, "protected": sorted(protected),
                      "expect": list(inspect_free(protected, SMALL_POOL_END))})

    # Allocator on its own.
    for count in (1, 3, 8, 300):
        for protected in (set(), set(range(2, 100)), set(range(0, 255, 3))):
            allocate = stage_allocator(protected, SMALL_POOL_END)
            result = outcome(lambda: [allocate(count), allocate(count)])
            cases.append({"kind": "allocator", "poolEnd": SMALL_POOL_END, "protected": sorted(protected), "count": count, "expect": result})

    # Import planning: report, consolidation cost and tokens.
    for label in ("small", "large"):
        data, pool_end = chain[label]
        snapshot_i = plan_vectors.snapshot_of(data, pool_end)
        owned_i = plan_vectors.owned(data, pool_end)
        for title, spec, protected in (("fits", one, owned_i), ("two-segment image", two, owned_i),
                                       ("no room for the image", one, set(range(pool_end + 1)) - {pool_end - 1, pool_end - 2, pool_end - 3})):
            record = {"kind": "importReport", "image": label, "title": title, "compiled": spec, "limit": 16, "protected": sorted(protected)}
            def report(spec=spec, protected=protected):
                compiled = build_compiled(spec)
                result, update = plan_compact_import(snapshot_i, protected, "Imported Snap", compiled, 16)
                return {"report": result, "update": serialise(update)}
            record["expect"] = outcome(report)
            cases.append(record)
        update = plan_named_snapshot(snapshot_i, owned_i, "Cost Probe", build_compiled(one), limit=16)
        after = metadata(update.payload, pool_end)
        for image_segments, index_records in ((1, (0, 0)), (1, (50, 50)), (1, (20000, 20000)), (4, (30000, 0)), (200, (0, 0)), (1, (90000, 90000))):
            cases.append({"kind": "consolidationCost", "image": label, "compiled": one, "limit": 16, "imageSegments": image_segments,
                          "indexRecords": list(index_records),
                          "expect": outcome(lambda: consolidation_cost(after, image_segments, index_records))})

    data, pool_end = chain["snaps"]
    snapshot_s = plan_vectors.snapshot_of(data, pool_end)
    owned_s = plan_vectors.owned(data, pool_end)
    compiled = build_compiled(one)
    digest = hashlib.sha256(compiled.data)
    digest.update(snapshot_s.payload)
    digest.update(json.dumps(["serial-123", 7, 3, sorted(owned_s)], separators=(",", ":")).encode())
    cases.append({"kind": "importToken", "image": "snaps", "compiled": one, "serial": "serial-123", "generation": 7, "tail": 3,
                  "protected": sorted(owned_s), "expect": digest.hexdigest()})
    cases.append({"kind": "offlineToken", "image": "snaps", "serial": "Sérial \"quoted\"", "upper": 11, "lower": 12,
                  "expect": offline_merge_token(snapshot_s, "Sérial \"quoted\"", 11, 12)})
    update = plan_change(snapshot_s, owned_s, snap_two, build_compiled(replacement), consolidate=True, merge_id=snap_one)
    fingerprint = hashlib.sha256(json.dumps(["serial-123", "OK 1 2 3 4", snap_two, snap_one, snapshot_s.highwater], separators=(",", ":")).encode())
    fingerprint.update(snapshot_s.payload)
    for segment, block in sorted(update.segments.items()):
        fingerprint.update(struct.pack("<II", segment, len(block)))
        fingerprint.update(block)
    cases.append({"kind": "mergeToken", "image": "snaps", "compiled": replacement, "serial": "serial-123", "answer": "OK 1 2 3 4",
                  "upper": snap_two, "lower": snap_one, "protected": sorted(owned_s), "expect": fingerprint.hexdigest()})

    # merged_entries replays flash reads, so it uses the decoder's device-shaped image.
    from golden_snapshot_plan_extra import generate_extra
    generate_extra(images, cases, chain, ids, replacement, dict(build_compiled=build_compiled, compiled_spec=compiled_spec, outcome=outcome, serialise=serialise))
    merged = decode_vectors_merged()
    return {"schema": 1, "images": images, "cases": cases, "merged": merged}


def decode_vectors_merged():
    """merged_entries over factory dictionaries that share keys (and one with overrides in the journal)."""
    case_list = []
    import tempfile
    from build_native_image import SOURCES, build
    rows = base_vectors.FACTORY + [(8, "Custom A", True), (9, "Custom B", False)]
    entries = {(1, (0x10,)): (0, b"hello"), (6, parse_outline("KAT")): (1, b""),
               (6, parse_outline("TKOG")): (0, b"dog (overridden)"), (6, (0x7000,)): (0, b"added to the base"),
               (2, parse_outline("KAT")): (0, b"override on another base")}
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        for _, name, _, relative in SOURCES:
            if relative:
                path = folder / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"KAT": name, "TKOG": "dog", "KAT/TKOG": name + "!"}))
        image, _ = build(folder, collection=(rows, entries, 10), pool_end=SMALL_POOL_END)
    image = bytearray(image)

    def run(title, upper_name, lower_name):
        address, size, crc = base_vectors.catalog_of(image)
        recorder = base_vectors.Recorder(image)
        record = {"title": title, "upper": upper_name, "lower": lower_name,
                  "catalog": {"address": address, "bytes": size, "crc": crc}}
        try:
            snapshot = Snapshot(recorder, address, size, crc, pool_end=SMALL_POOL_END)
            by_name = {row[16:16 + row[12]].decode(): row for row in snapshot.rows}
            for wanted in (upper_name, lower_name):
                if wanted not in by_name:
                    raise ValueError(f"No installed dictionary named '{wanted}'")
            merged, duplicates = merged_entries(snapshot, by_name[upper_name], by_name[lower_name], recorder)
            record["expect"] = {"entries": [list(pair) for pair in merged.items()], "duplicates": duplicates}
        except Exception as error:
            record["expect"] = {"error": str(error)}
        record["reads"] = [{"address": a, "length": n, "hex": d.rstrip(b"\xff").hex()} for a, n, d in recorder.reads]
        case_list.append(record)

    run("two factory dictionaries with the same keys", "Lapwing Commands", "Lapwing Numbers")
    run("override and reset in the journal", "Lapwing Base", "Lapwing Commands")
    run("a base with a journal-only dictionary", "Custom A", "Lapwing Base")
    run("unknown dictionary", "Lapwing Base", "Nothing")
    return case_list


def main():
    text = json.dumps(generate(), indent=1) + chr(10)
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8").replace(chr(13) + chr(10), chr(10)) != text:
            sys.exit("golden/snapshot_plan_vectors.json is stale; rerun golden_snapshot_plan.py")
        print("snapshot plan vectors are current")
        return
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline=chr(10))
    data = json.loads(text)
    print(f"wrote {OUT} ({len(text) // 1024} KiB, {len(data['cases']) + len(data['merged'])} cases)")


if __name__ == "__main__":
    main()
