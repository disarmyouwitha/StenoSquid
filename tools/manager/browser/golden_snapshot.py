#!/usr/bin/env python3
"""Generate (or check) golden vectors for the JavaScript snapshot reader (snapshot.js, Manager milestone M1).

Python's tools/storage/native_update.py is the oracle. For each case this records every flash read the Python
Snapshot makes (address, length, bytes) and what it concluded (or the error it raised), plus metadata-planner
results (move / rename / delete). test_snapshot.mjs replays the reads into the JS port and requires the same
result and the same read pattern.

    python3 tools/manager/browser/golden_snapshot.py            # rewrite golden/snapshot_vectors.json
    python3 tools/manager/browser/golden_snapshot.py --check    # fail if the committed file is stale
"""
import json
from pathlib import Path
import struct
import sys
import tempfile
import zlib

ROOT = Path(__file__).resolve().parents[3]
for sub in ("tools/storage", "tools/dictionaries", "tools/dict_compiler"):
    sys.path.insert(0, str(ROOT / sub))
from build_native_image import LARGE_POOL_END, SEGMENT, SMALL_POOL_END, SOURCES, build
from native_update import Edit, Snapshot, encode_record, metadata_payload

OUT = Path(__file__).with_name("golden") / "snapshot_vectors.json"
FACTORY = [(identity, name, enabled) for identity, name, enabled, _ in SOURCES]


def fixture(pool_end):
    """A device-shaped image: factory dictionaries, two independent named dictionaries and a personal journal."""
    rows = FACTORY + [(8, "Custom A", True), (9, "Custom B", False)]
    entries = {
        (1, (0x10,)): (0, b"hello"),
        (1, (0x10, 0x20)): (0, b"hello world"),
        (1, (0x30,)): (0, "café".encode()),
        (1, (0x40,)): (1, b""),
        (8, (0x50,)): (0, b"alpha"),
        (8, (0x60, 0x70, 0x80)): (0, b"beta gamma"),
        (9, (0x90,)): (0, b"zulu"),
    }
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        for _, name, _, relative in SOURCES:
            if relative:
                path = folder / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"KAT": name}))
        image, _ = build(folder, collection=(rows, entries, 10), pool_end=pool_end)
    return bytearray(image)


def catalog_of(image):
    size, crc = struct.unpack_from("<II", image, SEGMENT + 16)
    return SEGMENT + 32, size, crc


class Recorder:
    def __init__(self, image):
        self.image, self.reads = image, []

    def __call__(self, address, length):
        data = bytes(self.image[address:address + length])
        self.reads.append((address, length, data))
        return data


def summary(snapshot, metadata_only):
    out = {"next_id": snapshot.next_id, "personal": snapshot.personal, "rows": [row.hex() for row in snapshot.rows],
           "extents": [list(extent) for extent in snapshot.extents]}
    if not metadata_only:
        out["highwater"] = snapshot.highwater
        out["anchor"] = snapshot.anchor
        out["used"] = {str(segment): used for segment, used in snapshot.used.items()}
        out["entries"] = [[e.identity, list(e.strokes), e.address, e.operation, e.text.hex()]
                          for e in snapshot.entries.values()]
    return json.loads(json.dumps(out))  # tuples and large ints to plain JSON, huge u64 as numbers < 2**53 here


def run(name, image, pool_end, *, strict=False, metadata_only=False, catalog=None):
    address, size, crc = catalog or catalog_of(image)
    recorder = Recorder(image)
    case = {"name": name, "options": {"strict": strict, "metadataOnly": metadata_only, "poolEnd": pool_end},
            "catalog": {"address": address, "bytes": size, "crc": crc}}
    try:
        snapshot = Snapshot(recorder, address, size, crc, strict=strict, metadata_only=metadata_only, pool_end=pool_end)
        case["expect"] = summary(snapshot, metadata_only)
    except ValueError as error:
        snapshot = None
        case["expect"] = {"error": str(error)}
    case["reads"] = [{"address": a, "length": n, "hex": data.rstrip(b"\xff").hex()} for a, n, data in recorder.reads]
    return case, snapshot


def append_tail(image, pool_end, records, sequences=None):
    """Append journal records after the last committed one (sequence beyond the catalog checkpoint)."""
    address, size, crc = catalog_of(image)
    snapshot = Snapshot(lambda a, n: bytes(image[a:a + n]), address, size, crc, pool_end=pool_end)
    sequence, segment = snapshot.highwater, snapshot.anchor
    offset = segment * SEGMENT + 256 + snapshot.used[segment]
    for number, edit in enumerate(records):
        sequence = sequences[number] if sequences else sequence + 1
        blob = encode_record(edit, sequence)
        image[offset:offset + len(blob)] = blob
        offset += len(blob)


def metadata_cases(snapshot):
    operations = [
        ("move", ("Custom A", 1)), ("move", ("Custom A", 3)), ("move", ("Lapwing Base", 1)), ("move", ("Custom B", 99)),
        ("move", ("Custom B", 0)),
        ("move", ("Custom B", 9)), ("move", ("Custom B", 10)), ("move", ("Lapwing Base", 9)), ("move", ("Lapwing Base", 10)), ("move", ("Nothing", 1)), ("move", ("user_dictionary", 4)),
        ("rename", ("Custom A", "Renamed A")), ("rename", ("Custom A", "Custom A")), ("rename", ("Custom A", "Custom B")),
        ("rename", ("Custom A", "bad name ")), ("rename", ("Custom A", "")), ("rename", ("Custom A", "x" * 44)),
        ("rename", ("Custom A", "x" * 45)), ("rename", ("Custom A", ".dot")), ("rename", ("Lapwing Base", "New")),
        ("rename", ("user_dictionary", "New")), ("rename", ("Plover Base", "New")), ("rename", ("Nothing", "New")),
        ("delete", "Custom A"), ("delete", "Custom B"), ("delete", "Lapwing Base"), ("delete", "user_dictionary"),
        ("delete", "Nothing"), ("delete", "Plover Base"),
    ]
    cases = []
    for kind, argument in operations:
        case = {"op": kind, "args": argument if isinstance(argument, str) else list(argument)}
        try:
            payload = metadata_payload(snapshot, **{kind: argument})
            case["expect"] = {"payload": None if payload is None else payload.hex()}
        except ValueError as error:
            case["expect"] = {"error": str(error)}
        cases.append(case)
    return cases


def generate():
    cases = []
    for pool_end in (SMALL_POOL_END, LARGE_POOL_END):
        label = "small" if pool_end == SMALL_POOL_END else "large"
        base = fixture(pool_end)
        for strict in (False, True):
            case, _ = run(f"{label} pool clean, strict={strict}", base, pool_end, strict=strict)
            cases.append(case)
        case, snapshot = run(f"{label} pool metadata only", base, pool_end, metadata_only=True)
        case["metadata_cases"] = metadata_cases(snapshot)
        cases.append(case)

        tailed = bytearray(base)
        append_tail(tailed, pool_end, [
            Edit(0, 1, 0, (0x11,), b"new personal"),
            Edit(0, 1, 2, (0x10,), b""),               # delete a checkpointed entry
            Edit(0, 8, 0, (0x50,), b"alpha replaced"),  # replace a checkpointed entry
            Edit(0, 1, 1, (0x10, 0x20), b""),          # reset
            Edit(0, 777, 0, (0x99,), b"unknown identity"),
        ])
        for strict in (False, True):
            case, _ = run(f"{label} pool journal tail, strict={strict}", tailed, pool_end, strict=strict)
            cases.append(case)

    # Failure cases on the small pool, each changing one thing.
    base = fixture(SMALL_POOL_END)
    address, size, crc = catalog_of(base)
    _, snapshot = run("probe", base, SMALL_POOL_END)
    journal = next(extent for extent in snapshot.extents if extent[4] == 2)
    index = next(extent for extent in snapshot.extents if extent[4] == 3)
    journal_at = journal[0] * SEGMENT

    broken = bytearray(base); broken[journal_at + 256 + 40] ^= 0x01               # record body
    cases.append(run("corrupt record body", broken, SMALL_POOL_END)[0])
    broken = bytearray(base); broken[journal_at + 256 + 4] ^= 0x04                # record span
    cases.append(run("corrupt record span", broken, SMALL_POOL_END)[0])
    broken = bytearray(base); broken[journal_at + 3] ^= 0x01                      # segment header
    cases.append(run("corrupt journal header", broken, SMALL_POOL_END)[0])
    broken = bytearray(base); broken[journal_at + 17] ^= 0x01                     # header checksum
    cases.append(run("corrupt journal header checksum", broken, SMALL_POOL_END)[0])
    broken = bytearray(base); broken[index[0] * SEGMENT] ^= 0x01                  # forward index
    cases.append(run("corrupt forward index", broken, SMALL_POOL_END)[0])
    broken = bytearray(base); broken[address + 40] ^= 0x01                        # catalog payload
    cases.append(run("corrupt catalog", broken, SMALL_POOL_END)[0])
    cases.append(run("wrong catalog length", base, SMALL_POOL_END, catalog=(address, size, crc ^ 1))[0])

    shaped = bytearray(base)                                                      # row count out of range, valid CRC
    payload = bytearray(shaped[address:address + size]); struct.pack_into("<H", payload, 4, 65)
    shaped[address:address + size] = payload
    cases.append(run("bad catalog shape", shaped, SMALL_POOL_END, catalog=(address, size, zlib.crc32(bytes(payload))))[0])

    no_personal = bytearray(base)                                                 # rename the personal row
    payload = bytearray(no_personal[address:address + size]); payload[32 + 16] ^= 0x20
    no_personal[address:address + size] = payload
    cases.append(run("no personal dictionary", no_personal, SMALL_POOL_END, catalog=(address, size, zlib.crc32(bytes(payload))))[0])

    # Cases that pin individual checks (found by mutating the JS port: these paths were not covered).
    def with_catalog(image, change):
        copy = bytearray(image)
        payload = bytearray(copy[address:address + size]); change(payload)
        copy[address:address + size] = payload
        return copy, (address, size, zlib.crc32(bytes(payload)))

    last = int(snapshot.highwater)
    duplicate = bytearray(base)
    append_tail(duplicate, SMALL_POOL_END, [Edit(0, 1, 0, (0x11,), b"dup")], sequences=[last])
    cases.append(run("duplicate journal sequence", duplicate, SMALL_POOL_END)[0])

    for label, stroke_values, text in (("NUL in translation", (0x11,), b"a\0b"), ("stroke zero", (0,), b"x"),
                                       ("stroke at 2**25", (1 << 25,), b"x"), ("stroke just under 2**25", ((1 << 25) - 1,), b"x")):
        image = bytearray(base)
        append_tail(image, SMALL_POOL_END, [Edit(0, 1, 0, stroke_values, text)])
        cases.append(run(label, image, SMALL_POOL_END)[0])

    # A checkpoint boundary at the last indexed record: sequence == through must not be replayed as tail.
    tailed = bytearray(base)
    append_tail(tailed, SMALL_POOL_END, [Edit(0, 1, 0, (0x11,), b"boundary"), Edit(0, 1, 0, (0x12,), b"after")])
    moved, catalog = with_catalog(tailed, lambda payload: struct.pack_into("<Q", payload, 8, last + 1))
    for strict in (False, True):
        cases.append(run(f"checkpoint boundary inside the journal, strict={strict}", moved, SMALL_POOL_END,
                         strict=strict, catalog=catalog)[0])

    # A forward index that repeats an entry: equal keys are out of order in strict mode.
    twice = bytearray(base)
    first = bytes(twice[index[0] * SEGMENT:index[0] * SEGMENT + 4])
    twice[index[0] * SEGMENT + 4:index[0] * SEGMENT + 8] = first
    new_crc = zlib.crc32(bytes(twice[index[0] * SEGMENT:index[0] * SEGMENT + index[2]]))

    def patch_extent(payload):
        offset = 32 + struct.unpack_from("<H", payload, 4)[0] * 96
        for at in range(offset, len(payload), 24):
            if struct.unpack_from("<I", payload, at + 12)[0] == 3:
                struct.pack_into("<I", payload, at + 8, new_crc)
    repeated, catalog = with_catalog(twice, patch_extent)
    for strict in (False, True):
        cases.append(run(f"index repeats an entry, strict={strict}", repeated, SMALL_POOL_END, strict=strict, catalog=catalog)[0])
    return {"schema": 1, "cases": cases}


def main():
    text = json.dumps(generate(), indent=1) + "\n"
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8").replace("\r\n", "\n") != text:
            sys.exit("golden/snapshot_vectors.json is stale; rerun golden_snapshot.py")
        print("golden vectors are current")
        return
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUT} ({len(text) // 1024} KiB, {len(json.loads(text)['cases'])} cases)")


if __name__ == "__main__":
    main()
