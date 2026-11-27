"""Golden vectors for recovery.js (native_recovery.capture / reconstruct / digest).

The oracle is Python. One valid backup is captured from a device-shaped image; each case then damages it (a field, a catalog
byte, an extent byte with or without a repaired checksum), re-signs it (so the check under test is the one reached, not the
outer checksum) and records what reconstruct() does: the dictionaries, overlay records and extents it finds, a CRC of the
rebuilt flash image, or the exact failure. Capture cases record the reads it asks for and the bundle (or failure) it makes.
"""
import base64
import copy
import json
from pathlib import Path
import random
import struct
import sys
import zlib

import golden_decode as images  # also puts the tools/ directories on sys.path
import golden_snapshot as base_vectors
import native_recovery as recovery
from native_update import Snapshot

OUT = Path(__file__).with_name("golden") / "recovery_vectors.json"
SMALL = 255


def base_bundle():
    image, _ = images.effective_image()
    address, size, crc = base_vectors.catalog_of(image)
    probe = Snapshot(lambda a, n: bytes(image[a:a + n]), address, size, crc, metadata_only=True, pool_end=SMALL)
    bundle = recovery.capture(lambda a, n: bytes(image[a:a + n]), probe, "Mac U.S.")
    return image, probe, bundle


def special(name, bundle):
    """Large values are named in the vectors instead of repeated (recovery test resolves the same names)."""
    catalog, extents = bundle["catalog"], bundle["extents"]
    return {"huge": "A" * (((1023 * 65536 + 2) // 3 * 4) + 4), "catalog_cut": catalog[:-4], "catalog_more": catalog + "AAAA",
            "extents_less": extents[:-1], "extents_more": extents + ["AAAA"], "extent0_cut": extents[0][:-4],
            "extent0_bad": "!!!!" + extents[0][4:], "extent6_cut": extents[6][:-4]}[name]


def resolve(value, bundle):
    return special(value["$"], bundle) if isinstance(value, dict) and list(value) == ["$"] else value


def apply(bundle, ops):
    """The shared mutation language (recovery.js test applies the same operations)."""
    base_copy = bundle
    bundle = copy.deepcopy(bundle)
    resign = True
    for op in ops:
        kind = op[0]
        if kind == "set":
            target = bundle
            for key in op[1][:-1]:
                target = target[key]
            target[op[1][-1]] = resolve(op[2], base_copy)
        elif kind == "del":
            target = bundle
            for key in op[1][:-1]:
                target = target[key]
            del target[op[1][-1]]
        elif kind == "catalog":  # set one catalog byte
            payload = bytearray(base64.b64decode(bundle["catalog"]))
            payload[op[1]] = op[2]
            bundle["catalog"] = base64.b64encode(bytes(payload)).decode()
        elif kind == "extent":  # set one byte of an extent, optionally repairing its catalog checksum
            index, offset, value, fix = op[1:]
            data = bytearray(base64.b64decode(bundle["extents"][index]))
            data[offset] = value
            bundle["extents"][index] = base64.b64encode(bytes(data)).decode()
            if fix:
                payload = bytearray(base64.b64decode(bundle["catalog"]))
                rows = struct.unpack_from("<H", payload, 4)[0]
                struct.pack_into("<I", payload, 32 + rows * 96 + index * 24 + 8, zlib.crc32(bytes(data)))
                bundle["catalog"] = base64.b64encode(bytes(payload)).decode()
        elif kind == "nosign":
            resign = False
        else:
            raise ValueError(kind)
    if resign and isinstance(bundle, dict):
        bundle["sha256"] = recovery.digest(bundle)
    return bundle


def outcome(bundle):
    try:
        image, snapshot = recovery.reconstruct(bundle)
        return {"ok": {"rows": len(snapshot.rows), "entries": len(snapshot.entries), "extents": len(snapshot.extents),
                       "pool_end": snapshot.pool_end, "image_crc": zlib.crc32(image)}}
    except Exception as error:
        return {"error": {"type": type(error).__name__, "message": str(error)}}


def generate():
    image, probe, base = base_bundle()
    payload = base64.b64decode(base["catalog"])
    rows = struct.unpack_from("<H", payload, 4)[0]
    count = struct.unpack_from("<H", payload, 6)[0]
    extent_at = lambda i: 32 + rows * 96 + i * 24
    row_at = lambda i: 32 + i * 96
    cases = []

    def add(title, ops):
        cases.append({"title": title, "ops": ops, "expect": outcome(apply(base, ops))})

    add("the valid backup", [])
    add("an unmodified backup that was not re-signed", [["nosign"]])
    add("wrong checksum", [["set", ["sha256"], "0" * 64], ["nosign"]])
    add("checksum missing", [["del", ["sha256"]], ["nosign"]])
    add("wrong format", [["set", ["format"], "stenosquid-backup-1"]])
    add("format missing", [["del", ["format"]]])
    for layout in ("Windows U.S. (plain text)", "Linux", "", None, 5):
        add(f"host layout {layout!r}", [["set", ["host_layout"], layout]])
    add("host layout missing", [["del", ["host_layout"]]])
    for value in (["x"], None, 5, "x"):
        cases.append({"title": f"the bundle is {value!r}", "bundle": value, "ops": [], "expect": outcome(value)})

    identity = {"serial": "ABC123", "generation": 7, "timestamp": "2026-01-01T00:00:00+00:00"}
    add("a valid capture identity", [["set", ["capture"], identity]])
    for key, values in (("serial", ["", None, 5, ["x"]]), ("generation", [0, -1, "1", None, 1.5, True, False, 2 ** 40, [1]]),
                        ("timestamp", ["", None, 5])):
        for value in values:
            add(f"capture {key} = {value!r}", [["set", ["capture"], {**identity, key: value}]])
        add(f"capture {key} missing", [["set", ["capture"], {k: v for k, v in identity.items() if k != key}]])
    for value in (None, [], "x", 5):
        add(f"capture = {value!r}", [["set", ["capture"], value]])

    for value in (1023, 255, 256, 0, None, "255", True, 100, -1, 1.5):
        add(f"pool_segments = {value!r}", [["set", ["pool_segments"], value]])

    for value in (None, 5, [], "", "!!!!", "AAA", "AA=A", {"$": "huge"}, "AAAA", {"$": "catalog_cut"}, {"$": "catalog_more"}):
        add(f"catalog = {str(value)[:20]!r} ({type(value).__name__})", [["set", ["catalog"], value]])
    add("catalog missing", [["del", ["catalog"]]])
    add("extents missing", [["del", ["extents"]]])
    for value in (None, "x", {}, 5):
        add(f"extents = {value!r}", [["set", ["extents"], value]])
    add("one extent too few", [["set", ["extents"], {"$": "extents_less"}]])
    add("one extent too many", [["set", ["extents"], {"$": "extents_more"}]])
    add("an extent that is not a string", [["set", ["extents", 0], 5]])
    add("an extent of the wrong size", [["set", ["extents", 0], {"$": "extent0_cut"}]])
    add("an extent with bad base64", [["set", ["extents", 0], {"$": "extent0_bad"}]])
    add("the journal extent shortened", [["set", ["extents", 6], {"$": "extent6_cut"}]])
    add("an extra unknown key", [["set", ["note"], "hello"]])
    add("an unknown key with non-ASCII text", [["set", ["note"], "café 日本 \U0001F600"]])
    add("keys with odd characters", [["set", ["zé"], 1], ["set", ["a\"b"], 2], ["set", ["tab\there"], [1, None, True]]])

    # catalog header and extent descriptors
    for title, offset, value in (("next_id below 8", 0, 7), ("more than 64 rows", 5, 1), ("row count too low", 4, rows - 1),
                                 ("extent count too high", 6, count + 1), ("extent count too low", 6, count - 1),
                                 ("reserved bytes used", 16, 1), ("reserved bytes used at the end", 31, 9)):
        add(f"catalog: {title}", [["catalog", offset, value]])
    descriptor_cases = (
        ("start below 2", 0, 1), ("start at the pool end", 0, 255), ("start past the pool end", 1, 1), ("span 0", 2, 0),
        ("span too large", 3, 1), ("size 0", 4, 0), ("size over its span", 5, 5), ("crc changed", 8, 1), ("kind 0", 12, 0),
        ("kind 5", 12, 5), ("kind huge", 13, 1), ("epoch set on a compact extent", 16, 1))
    for i in range(count):
        for title, offset, value in descriptor_cases:
            add(f"extent {i}: {title}", [["catalog", extent_at(i) + offset, value]])
    for i in range(count):  # every extent onto its neighbour's start, and every kind swapped
        for other in range(count):
            if other != i:
                add(f"extent {i} placed on extent {other}", [["catalog", extent_at(i), payload[extent_at(other)]]])
        for kind in (1, 2, 3, 4):
            add(f"extent {i} given kind {kind}", [["catalog", extent_at(i) + 12, kind]])
        for epoch in (0, 2, 255):
            add(f"extent {i} given epoch {epoch}", [["catalog", extent_at(i) + 16, epoch]])

    # dictionary rows
    row_cases = (
        ("identity 0", 0, 0), ("identity beyond next_id", 0, 200), ("identity 8 (not yet used)", 0, 8), ("enabled 2", 4, 2),
        ("enabled huge", 7, 1), ("base points at extent 0", 8, 0), ("base points at the journal", 8, 6), ("base past the extents", 8, 40),
        ("base 65535 (journal dictionary)", 8, 255), ("member 1", 10, 1), ("marker 1", 11, 1), ("marker 2", 11, 2), ("marker 3", 11, 3),
        ("name length 0", 12, 0), ("name length 1", 12, 1), ("name length 45", 12, 45), ("name length 60", 12, 60),
        ("reserved byte 13", 13, 1), ("reserved byte 15", 15, 1), ("garbage after the name", 63, 1),
        ("pinned hash cleared", 64, 0), ("pinned hash set", 95, 1), ("name changed", 16, 90), ("name made non-UTF-8", 16, 255))
    for i in range(rows):
        for title, offset, value in row_cases:
            add(f"row {i}: {title}", [["catalog", row_at(i) + offset, value]])
        for other in range(rows):
            if other != i:
                add(f"row {i} takes the identity of row {other}", [["catalog", row_at(i), payload[row_at(other)]]])
    # a user dictionary that clashes with a factory name, or is named oddly
    name_at = row_at(rows - 1) + 16
    for title, text in (("a factory name", b"Plover Base"), ("a reserved-looking name", b"user_dictionary"),
                        ("an empty-looking name", b"        "), ("a control character", b"Cus\x01om B"),
                        ("a slash", b"Custom/B"), ("a long name", b"x" * 44)):
        ops = [["catalog", name_at + i, text[i] if i < len(text) else 0] for i in range(44)]
        ops.append(["catalog", row_at(rows - 1) + 12, len(text)])
        add(f"last row named {title}", ops)

    # extent bytes, with a repaired checksum so the deeper checks are reached
    rng = random.Random(20261126)
    sizes = [len(base64.b64decode(e)) for e in base["extents"]]
    for index, size in enumerate(sizes):
        if index == 6:
            for offset in (0, 7, 8, 15, 16, 19, 255, 256, 260, 284, 288, 300, 1000):
                add(f"journal extent byte {offset}", [["extent", index, offset, 0x5a, False]])
            continue
        picks = sorted({0, 1, 3, 4, 5, 8, 12, 13, 16, 20, 31, 32, 35, 36, 39, 40, size - 1, size // 2}
                       | {rng.randrange(size) for _ in range(40)})
        for offset in (p for p in picks if 0 <= p < size):
            for value in (0, 0xff, 0x5a):
                add(f"extent {index} byte {offset} = {value:#x}, checksum repaired", [["extent", index, offset, value, True]])
        add(f"extent {index} byte {size // 2} changed, checksum left", [["extent", index, size // 2, 0x5a, False]])
    # random combinations of catalog bytes
    for number in range(150):
        ops = [["catalog", rng.randrange(len(payload)), rng.randrange(256)] for _ in range(rng.choice((1, 1, 2, 3)))]
        add(f"random catalog damage {number}", ops)

    # ---- capture ----
    captures = []
    reads = []

    def read_for(tweaks):
        def read(start, size):
            index = len(reads)
            data = bytes(image[start:start + size])
            tweak = tweaks.get(index)
            if tweak:
                if "raise" in tweak:
                    raise RuntimeError(tweak["raise"])
                if "flip" in tweak:
                    data = bytes(b ^ 0x5a if i == tweak["flip"] else b for i, b in enumerate(data))
                if "cut" in tweak:
                    data = data[:-tweak["cut"]]
            reads.append({"start": start, "size": size, "hex": data.rstrip(bytes([255])).hex(), "full": len(data)})
            return data
        return read

    def capture_case(title, layout, tweaks=None, pool_end=SMALL):
        reads.clear()
        snapshot = Snapshot(lambda a, n: bytes(image[a:a + n]), *base_vectors.catalog_of(image), metadata_only=True, pool_end=pool_end)
        try:
            result = {"bundle": recovery.capture(read_for(tweaks or {}), snapshot, layout)}
        except Exception as error:
            result = {"error": {"type": type(error).__name__, "message": str(error)}}
        captures.append({"title": title, "layout": layout, "tweaks": [[k, v] for k, v in sorted((tweaks or {}).items())],
                         "reads": copy.deepcopy(reads), "expect": result})

    capture_case("a clean capture", "Mac U.S.")
    capture_case("a clean capture, other layout", "Windows U.S. (plain text)")
    capture_case("an unsupported layout", "Linux")
    capture_case("an empty layout", "")
    for index in range(count):
        capture_case(f"read {index} fails", "Mac U.S.", {index: {"raise": "ERR Native flash busy"}})
        capture_case(f"read {index} is short by one byte", "Mac U.S.", {index: {"cut": 1}})
        capture_case(f"read {index} has a flipped byte", "Mac U.S.", {index: {"flip": 3}})
    return {"schema": 1, "base": base, "cases": cases, "captures": captures}


def main():
    text = json.dumps(generate(), indent=0) + chr(10)
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8").replace(chr(13) + chr(10), chr(10)) != text:
            sys.exit("golden/recovery_vectors.json is stale; rerun golden_recovery.py")
        print("recovery vectors are current")
        return
    OUT.write_text(text, encoding="utf-8", newline=chr(10))
    data = json.loads(text)
    print(f"wrote {OUT} ({len(text) // 1024} KiB, {len(data['cases'])} reconstruct cases, {len(data['captures'])} captures)")


if __name__ == "__main__":
    main()
