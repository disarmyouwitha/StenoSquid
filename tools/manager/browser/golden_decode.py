#!/usr/bin/env python3
"""Generate (or check) golden vectors for the JavaScript dictionary decoder (decode.js, Manager milestone M1).

The oracle is Python: hybrid.compact_entries, jsc4.require_snapshot_hint and native_update.effective_entries. Cases:
  * decode: compiled JSC4 images (single and two-member, with and without a flash base address), then the same images
    with single-byte damage and truncation; the result is the entry list or the error type and message.
  * hint: the SHI1 snapshot-hint check on a compiled snapshot, intact and damaged.
  * effective: effective_entries on a device-shaped image (factory bases plus journal overrides and resets, a checksum
    failure, an unknown name), replaying the exact flash reads.

    python3 tools/manager/browser/golden_decode.py            # rewrite golden/decode_vectors.json
    python3 tools/manager/browser/golden_decode.py --check    # fail if the committed file is stale
"""
import hashlib
import json
from pathlib import Path
import random
import sys
import tempfile
import zlib

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import golden_snapshot as base_vectors  # also puts the tools/ directories on sys.path
from build_native_image import SEGMENT, SMALL_POOL_END, SOURCES, XIP, build
from compile_dict import DictionaryCollectionBuilder, parse_outline
from hybrid import compact_entries
from jsc4 import require_snapshot_hint
from native_snapshot import compile_snapshot
from native_update import Snapshot, effective_entries

OUT = HERE / "golden" / "decode_vectors.json"
LEFT, MIDDLE, RIGHT = "STKPWHR", "AO*EU", "FRPBLGTSDZ"


def outline(rng, strokes):
    parts = []
    while len(parts) < strokes:
        left = "".join(c for c in LEFT if rng.random() < 0.3)
        middle = "".join(c for c in MIDDLE if rng.random() < 0.3)
        right = "".join(c for c in RIGHT if rng.random() < 0.3)
        if left or middle or right:
            parts.append(left + middle + ("-" if right and not middle else "") + right)
    return "/".join(parts)


def dictionary(seed, count):
    rng = random.Random(seed)
    words = ["cat", "dog", "café", "naïve", "{^ing}", "{-|}", "über", "x" * 40, "hello world", "日本語", "a", "{.}"]
    entries = {}
    while len(entries) < count:
        entries[outline(rng, rng.choice((1, 1, 1, 2, 2, 3, 4)))] = rng.choice(words) + ("" if rng.random() < 0.7 else str(rng.randrange(1000)))
    return entries


def digest(entries):
    return hashlib.sha256(chr(10).join(f"{outline}{chr(9)}{text}" for outline, text in entries).encode("utf-8")).hexdigest()


def outcome(function):
    try:
        return {"entries": [list(pair) for pair in function()]}
    except Exception as error:  # the oracle's own failure mode is the expectation
        return {"error": {"type": type(error).__name__, "message": str(error)}}


def decode_case(title, blob, base, member, count, flips=(), cut=None):
    data = bytearray(blob)
    for offset, mask in flips:
        data[offset] ^= mask
    if cut is not None:
        data = data[:cut]
    case = {"name": title, "blob": None, "base": base, "member": member, "count": count, "flips": [list(f) for f in flips], "cut": cut}
    case["expect"] = outcome(lambda: compact_entries(bytes(data), base, member, count))
    if "entries" in case["expect"] and (flips or cut is not None):
        # Damaged-but-still-decodable images: a digest keeps the file small (same rule in test_decode.mjs).
        case["expect"] = {"count": len(case["expect"]["entries"]), "sha256": digest(case["expect"]["entries"])}
    return case


def generate_decode():
    blobs, cases = {}, []
    single = DictionaryCollectionBuilder(0).build([("Solo", dictionary(1, 300))])
    pair = DictionaryCollectionBuilder(XIP + 0x30000).build([("First", dictionary(2, 260)), ("Second", dictionary(3, 180))])
    tiny = DictionaryCollectionBuilder(XIP).build([("Tiny", {"KAT": "cat", "TKOG": "dog", "KAT/TKOG": "cat dog"})])
    blobs.update(single=bytes(single), pair=bytes(pair), tiny=bytes(tiny))

    def add(label, blob_name, base, member, count, **kwargs):
        case = decode_case(label, blobs[blob_name], base, member, count, **kwargs)
        case["blob"] = blob_name
        cases.append(case)

    add("single member, base 0", "single", 0, 0, 1)
    add("pair, first member", "pair", XIP + 0x30000, 0, 2)
    add("pair, second member", "pair", XIP + 0x30000, 1, 2)
    add("pair, member out of range", "pair", XIP + 0x30000, 2, 2)
    add("pair, wrong member count", "pair", XIP + 0x30000, 0, 1)
    add("wrong base address", "single", 4096, 0, 1)
    add("tiny", "tiny", XIP, 0, 1)

    rng = random.Random(99)
    for name in ("tiny", "single", "pair"):
        blob = blobs[name]
        base = {"tiny": XIP, "single": 0, "pair": XIP + 0x30000}[name]
        count = 2 if name == "pair" else 1
        header = list(range(0, min(len(blob), 160)))
        scattered = sorted(rng.sample(range(len(blob)), min(len(blob), 140 if name != "tiny" else 60)))
        for offset in header + scattered:
            add(f"{name}: flip byte {offset}", name, base, 0, count, flips=[(offset, 1 << rng.randrange(8))])
        for cut in (0, 3, 4, 35, 36, 40, 59, 60, len(blob) // 2, len(blob) - 4, len(blob) - 1):
            add(f"{name}: truncated to {cut}", name, base, 0, count, cut=cut)
    # Targeted damage: maximum stroke length, bitmap mask, and the very end of the image (found by mutating the port).
    for name in ("tiny", "single"):
        blob, base = blobs[name], {"tiny": XIP, "single": 0}[name]
        definition = int.from_bytes(blob[36:40], "little") - base
        table = int.from_bytes(blob[definition + 12:definition + 16], "little") - base
        for value in (0, 1, 16, 17, 32, 255):
            add(f"{name}: maximum length set to {value}", name, base, 0, 1, flips=[(definition + 1, blob[definition + 1] ^ value)])
        for mask in (0x7F, 0xFF, 0x3FF, 0xFFFFF, 0x7FFFFFFF):
            current = int.from_bytes(blob[table:table + 4], "little")
            delta = (current ^ mask).to_bytes(4, "little")
            add(f"{name}: bitmap mask set to {mask:#x}", name, base, 0, 1, flips=[(table + i, delta[i]) for i in range(4)])
        for back in range(1, 41):
            add(f"{name}: truncated {back} bytes before the end", name, base, 0, 1, cut=len(blob) - back)
    return {"blobs": {key: value.hex() for key, value in blobs.items()}, "cases": cases}


def generate_hint():
    snapshot = bytes(compile_snapshot("Snap", {(0x10,): b"hello", (0x10, 0x20): b"hello world", (0x44,): "café".encode()}).data)
    cases = []

    def add(title, flips=(), cut=None, base=0):
        data = bytearray(snapshot)
        for offset, mask in flips:
            data[offset] ^= mask
        if cut is not None:
            data = data[:cut]
        case = {"name": title, "flips": [list(f) for f in flips], "cut": cut, "base": base}
        try:
            require_snapshot_hint(bytes(data), base)
            case["expect"] = {"ok": True}
        except Exception as error:
            case["expect"] = {"error": {"type": type(error).__name__, "message": str(error)}}
        cases.append(case)

    add("intact")
    add("not a multiple of four", cut=len(snapshot) - 1)
    add("too short", cut=40)
    for offset in (0, 3, 4, 8, 9, 12, 13):
        add(f"header byte {offset}", flips=[(offset, 1)])
    for back in range(1, 25):
        add(f"{back} bytes from the end", flips=[(len(snapshot) - back, 1)])
    for cut in (52, 56, 60, 64):
        add(f"cut to {cut} bytes", cut=cut)
    add("wrong base", base=64)
    add("base beyond the text pointer", base=1 << 30)
    add("base just under the text pointer", base=int.from_bytes(snapshot[12:16], "little") + 1)
    return {"blob": snapshot.hex(), "cases": cases}


def effective_image():
    """A device-shaped image (factory bases plus journal overrides and resets) and its dictionary rows."""
    rows = base_vectors.FACTORY + [(8, "Custom A", True), (9, "Custom B", False)]
    kat, dog = parse_outline("KAT"), parse_outline("TKOG")
    entries = {
        (1, (0x10,)): (0, b"hello"), (1, (0x10, 0x20)): (0, b"hello world"), (1, (0x30,)): (0, "café".encode()),
        (1, (0x40,)): (1, b""),
        (6, kat): (1, b""),                    # reset a base entry
        (6, dog): (0, b"dog (overridden)"),    # replace a base entry
        (6, (0x7000,)): (0, b"added to the base"),
        (2, kat): (0, b"override on another base"),
        (8, (0x50,)): (0, b"alpha"), (8, (0x60, 0x70)): (0, b"beta"),
    }
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        for _, name, _, relative in SOURCES:
            if relative:
                path = folder / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"KAT": name, "TKOG": "dog", "KAT/TKOG": name + "!"}))
        image, _ = build(folder, collection=(rows, entries, 10), pool_end=SMALL_POOL_END)
    image = bytearray(image)
    return image, rows


def generate_effective():
    image, rows = effective_image()
    kat, dog = parse_outline("KAT"), parse_outline("TKOG")

    def run(title, data, name):
        address, size, crc = base_vectors.catalog_of(data)
        recorder = base_vectors.Recorder(data)
        case = {"name": title, "target": name, "catalog": {"address": address, "bytes": size, "crc": crc}}
        try:
            snapshot = Snapshot(recorder, address, size, crc, pool_end=SMALL_POOL_END)
            case["expect"] = {"entries": [list(pair) for pair in effective_entries(snapshot, name, recorder).items()]}
        except Exception as error:
            case["expect"] = {"error": {"type": type(error).__name__, "message": str(error)}}
        case["reads"] = [{"address": a, "length": n, "hex": d.rstrip(b"\xff").hex()} for a, n, d in recorder.reads]
        return case

    cases = [run(f"effective entries of {name}", image, name) for _, name, _ in rows]
    cases.append(run("unknown dictionary", image, "Nothing"))
    address, size, crc = base_vectors.catalog_of(image)
    probe = Snapshot(lambda a, n: bytes(image[a:a + n]), address, size, crc, metadata_only=True, pool_end=SMALL_POOL_END)
    base_segment = probe.extents[0][0]  # the first factory base image
    damaged = bytearray(image)
    damaged[base_segment * SEGMENT + 20] ^= 1
    cases.append(run("base image fails its checksum", damaged, "Lapwing Commands"))
    # Catalog rows the factory fixture does not produce: a member index, a snapshot flag, a base that does not exist.
    def patched(title, name, changes):
        import struct
        data = bytearray(image)
        address, size, _ = base_vectors.catalog_of(data)
        payload = bytearray(data[address:address + size])
        index = [n for _, n, _ in rows].index(name)
        for offset, value in changes.items():
            payload[32 + index * 96 + offset] = value
        data[address:address + size] = payload
        struct.pack_into("<I", data, SEGMENT + 20, zlib.crc32(bytes(payload)))
        cases.append(run(title, data, name))

    patched("row names member 1 of a one-member image", "Lapwing Commands", {10: 1})
    patched("row flagged as a snapshot over a plain base", "Lapwing Commands", {11: 1})
    patched("row points at a base extent past the catalog", "Lapwing Commands", {8: 200, 9: 0})
    return {"cases": cases}


def generate():
    return {"schema": 1, "decode": generate_decode(), "hint": generate_hint(), "effective": generate_effective()}


def main():
    text = json.dumps(generate(), indent=0) + chr(10)
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8").replace(chr(13) + chr(10), chr(10)) != text:
            sys.exit("golden/decode_vectors.json is stale; rerun golden_decode.py")
        print("decode vectors are current")
        return
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline=chr(10))
    data = json.loads(text)
    total = len(data["decode"]["cases"]) + len(data["hint"]["cases"]) + len(data["effective"]["cases"])
    print(f"wrote {OUT} ({len(text) // 1024} KiB, {total} cases)")


if __name__ == "__main__":
    main()
