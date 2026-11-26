#!/usr/bin/env python3
"""Generate (or check) golden vectors for the JavaScript update planner (plan.js, Manager milestone M2).

Python's plan() in tools/storage/native_update.py is the oracle. Each image is recorded once (every flash read the
Python Snapshot makes); each case records the arguments and the resulting Update: metadata segment, payload bytes and,
per staged segment, its length and SHA-256, in insertion order (the upload order). test_plan.mjs rebuilds the snapshot
from the recorded reads, runs the JS planner and requires byte-identical results, or the same error.

    python3 tools/manager/browser/golden_plan.py            # rewrite golden/plan_vectors.json
    python3 tools/manager/browser/golden_plan.py --check    # fail if the committed file is stale
"""
import hashlib
import json
from pathlib import Path
import sys
import tempfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import golden_snapshot as base_vectors  # also puts the tools/ directories on sys.path
from build_native_image import LARGE_POOL_END, SEGMENT, SMALL_POOL_END, SOURCES, build
from native_update import Edit, NoSpace, Snapshot, plan

OUT = HERE / "golden" / "plan_vectors.json"
BIG_TEXT = b"x" * 1500


def big_fixture(pool_end):
    """About 130 large personal entries over several journal segments, then a tail that replaces most of the first
    70, leaving two old journals mostly dead (so compaction has something to reclaim)."""
    rows = base_vectors.FACTORY + [(8, "Custom A", True)]
    entries = {(1, (0x1000 + i,)): (0, b"%03d" % i + BIG_TEXT) for i in range(130)}
    entries[(8, (0x50,))] = (0, b"alpha")
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        for _, name, _, relative in SOURCES:
            if relative:
                path = folder / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"KAT": name}))
        image, _ = build(folder, collection=(rows, entries, 9), pool_end=pool_end)
    image = bytearray(image)
    base_vectors.append_tail(image, pool_end, [Edit(0, 1, 0, (0x1000 + i,), b"s%d" % i) for i in range(70)])
    return image


def owned(image, pool_end):
    address, size, crc = base_vectors.catalog_of(image)
    probe = Snapshot(lambda a, n: bytes(image[a:a + n]), address, size, crc, metadata_only=True, pool_end=pool_end)
    segments = {0, 1}
    for start, span, *_ in probe.extents:
        segments.update(range(start, start + span))
    return segments


def record_image(image, pool_end):
    address, size, crc = base_vectors.catalog_of(image)
    recorder = base_vectors.Recorder(image)
    Snapshot(recorder, address, size, crc, pool_end=pool_end)
    return {"poolEnd": pool_end, "catalog": {"address": address, "bytes": size, "crc": crc},
            "reads": [{"address": a, "length": n, "hex": data.rstrip(b"\xff").hex()} for a, n, data in recorder.reads],
            "protected": sorted(owned(image, pool_end))}


def snapshot_of(image, pool_end):
    address, size, crc = base_vectors.catalog_of(image)
    return Snapshot(lambda a, n: bytes(image[a:a + n]), address, size, crc, pool_end=pool_end)


def serialise(update):
    if update is None:
        return {"update": None}
    return {"update": {"metadata": update.metadata, "payload": update.payload.hex(), "reclaimed": update.reclaimed,
                       "changes": update.changes,
                       "segments": [[segment, len(block), hashlib.sha256(bytes(block)).hexdigest()]
                                    for segment, block in update.segments.items()]}}


def case(title, image_name, snapshot, protected, **arguments):
    incoming = arguments.pop("incoming", None)
    record = {"name": title, "image": image_name, "protected": sorted(protected),
              "incoming": None if incoming is None else [[list(strokes), text.hex()] for strokes, text in incoming.items()],
              "arguments": {key: (list(value) if isinstance(value, tuple) else value) for key, value in arguments.items()}}
    try:
        record["expect"] = serialise(plan(snapshot, protected, incoming, **arguments))
    except NoSpace as error:
        record["expect"] = {"error": str(error), "kind": "NoSpace"}
    except ValueError as error:
        record["expect"] = {"error": str(error), "kind": "ValueError"}
    return record


def generate():
    images, cases = {}, []
    sources = {"small": (base_vectors.fixture(SMALL_POOL_END), SMALL_POOL_END),
               "large": (base_vectors.fixture(LARGE_POOL_END), LARGE_POOL_END),
               "big": (big_fixture(SMALL_POOL_END), SMALL_POOL_END)}
    for label, (image, pool_end) in sources.items():
        images[label] = record_image(image, pool_end)
        snapshot, protected = snapshot_of(image, pool_end), owned(image, pool_end)
        everything = set(range(pool_end + 1))

        def add(title, **arguments):
            cases.append(case(f"{label}: {title}", label, snapshot, arguments.pop("protected", protected), **arguments))

        if label != "big":
            add("personal edits (new, changed, unchanged, reset entry)", incoming={
                (0x10,): b"hello", (0x11,): b"brand new", (0x30,): b"changed", (0x40,): b"was reset", (0x10, 0x20): b"hello world"})
            add("personal edits, nothing changed", incoming={(0x10,): b"hello", (0x30,): "café".encode()})
            add("empty incoming", incoming={})
            add("many new personal entries (journal rollover)", incoming={(0x2000 + i,): b"entry %d" % i + b"y" * 900 for i in range(120)})
            add("named import", incoming={(0x10,): b"hello", (0x20, 0x30): b"two strokes"}, name="My Names")
            add("named import, empty", incoming={}, name="Empty Dict")
            add("named import, duplicate name", incoming={(0x10,): b"a"}, name="Custom A")
            add("named import, invalid name", incoming={(0x10,): b"a"}, name="bad name ")
            add("named import, dictionary limit", incoming={(0x10,): b"a"}, name="Another", limit=len(snapshot.rows))
            add("compact a clean journal", incoming=None)
            add("move", move=("Custom A", 1))
            add("move, unchanged position", move=("Lapwing Base", 6))
            add("rename", rename=("Custom A", "Renamed A"))
            add("delete", delete="Custom B")
            add("delete, not independent", delete="Lapwing Base")
            add("no staging space", incoming={(0x11,): b"x"}, protected=everything - {pool_end - 1, pool_end - 2})
            add("no staging space, metadata edit", move=("Custom A", 1), protected=everything - {pool_end - 1, pool_end - 2})
            add("no staging space, only the reserve free", move=("Custom A", 1), protected=everything - {pool_end - 3, pool_end - 2, pool_end - 1})
        else:
            add("compact two mostly-dead journals", incoming=None)
            add("personal edit on a multi-journal catalog", incoming={(0x1000 + 90,): b"changed", (0x5000,): b"new"})
            add("named import on a multi-journal catalog", incoming={(0x10,): b"a"}, name="Side Dictionary")
            add("compact, no room for the copy and indexes", incoming=None, protected=everything - {pool_end - 1, pool_end - 2, pool_end - 3, pool_end - 4})
    # Boundary cases found by mutating the JS port (each pins one comparison).
    small_image, small_end = sources["small"]
    small_snapshot = snapshot_of(small_image, small_end)
    small_owned = owned(small_image, small_end)
    everything = set(range(small_end + 1))

    def add_small(title, protected, **arguments):
        cases.append(case(f"small: {title}", "small", small_snapshot, protected, **arguments))

    top = small_end
    add_small("metadata edit with exactly four free segments", everything - {top - 4, top - 3, top - 2, top - 1}, move=("Custom A", 1))
    add_small("metadata edit with three free segments plus the pool end", everything - {top - 3, top - 2, top - 1, top}, move=("Custom A", 1))
    add_small("metadata edit with three free low segments plus segment 1", everything - {1, 2, 3, 4}, move=("Custom A", 1))
    add_small("metadata edit with nothing protected (lowest segment wins)", set(), move=("Custom A", 1))
    add_small("personal edit with nothing protected", set(), incoming={(0x11,): b"brand new"})
    add_small("reset entry set again to the same (empty) text", small_owned, incoming={(0x40,): b""})
    add_small("two-segment index run needs a contiguous gap", small_owned | {20}, incoming={(0x2000 + i,): b"e%d" % i + b"z" * 900 for i in range(200)})

    maxid = bytearray(small_image)
    address, size, _ = base_vectors.catalog_of(maxid)
    payload = bytearray(maxid[address:address + size])
    payload[0:4] = bytes([255, 255, 255, 255])
    maxid[address:address + size] = payload
    import struct
    import zlib
    struct.pack_into("<I", maxid, small_end and 65536 + 20, zlib.crc32(bytes(payload)))
    images["maxid"] = record_image(maxid, small_end)
    maxid_snapshot = snapshot_of(maxid, small_end)
    cases.append(case("maxid: named import when the identity counter is exhausted", "maxid", maxid_snapshot,
                      owned(maxid, small_end), incoming={(0x10,): b"a"}, name="Last One"))

    # Three candidate journals with live bytes of about 20, 30 and 50 KB: which two are reclaimed depends on the order.
    rows = base_vectors.FACTORY + [(8, "Custom A", True)]
    entries = {(1, (0x1000 + i,)): (0, b"%03d" % i + BIG_TEXT) for i in range(130)}
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        for _, name, _, relative in SOURCES:
            if relative:
                path = folder / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"KAT": name}))
        ordered, _ = build(folder, collection=(rows, entries, 9), pool_end=SMALL_POOL_END)
    ordered = bytearray(ordered)
    kill = list(range(13, 42)) + list(range(42 + 19, 84)) + list(range(84 + 32, 126))
    base_vectors.append_tail(ordered, SMALL_POOL_END, [Edit(0, 1, 0, (0x1000 + i,), b"s%d" % i) for i in kill])
    images["ordered"] = record_image(ordered, SMALL_POOL_END)
    cases.append(case("ordered: compaction picks the least-live journals first", "ordered", snapshot_of(ordered, SMALL_POOL_END),
                      owned(ordered, SMALL_POOL_END), incoming=None))
    return {"schema": 1, "images": images, "cases": cases}


def main():
    text = json.dumps(generate(), indent=1) + chr(10)
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8").replace(chr(13) + chr(10), chr(10)) != text:
            sys.exit("golden/plan_vectors.json is stale; rerun golden_plan.py")
        print("plan vectors are current")
        return
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline=chr(10))
    data = json.loads(text)
    print(f"wrote {OUT} ({len(text) // 1024} KiB, {len(data['cases'])} cases)")


if __name__ == "__main__":
    main()
