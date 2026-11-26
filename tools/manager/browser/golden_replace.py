#!/usr/bin/env python3
"""Generate (or check) golden vectors for the JavaScript JSC4 lookup reader and base-replacement planner
(lookup.js and replace_plan.js, Manager milestones M1/M2).

Oracle: jsc4.reader and parse_outline (lookup) and plan_replacements in tools/storage/native_base_update.py. Lookup cases
cover every entry of real compiled images (hits), misses, invalid outlines, and damaged images; replacement cases cover
changed and unchanged digests, journal overrides that conflict or do not, every refusal, and tight free space, with the
flash reads of the conflict check replayed.

    python3 tools/manager/browser/golden_replace.py            # rewrite golden/replace_vectors.json
    python3 tools/manager/browser/golden_replace.py --check    # fail if the committed file is stale
"""
import hashlib
import json
from pathlib import Path
import random
import struct
import sys
import tempfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import golden_decode as decode_vectors
import golden_plan as plan_vectors
import golden_snapshot as base_vectors  # also puts the tools/ directories on sys.path
import golden_snapshot_plan_extra as patches
from build_native_image import SEGMENT, SMALL_POOL_END, SOURCES, XIP, build
from compile_dict import DictionaryCollectionBuilder, parse_outline
from jsc4 import reader
from native_base_update import Image, plan_replacements
from native_update import NoSpace, Snapshot

OUT = HERE / "golden" / "replace_vectors.json"


def attempt(function):
    try:
        return {"value": function()}
    except NoSpace as error:
        return {"error": str(error), "type": "NoSpace"}
    except Exception as error:
        return {"error": str(error), "type": type(error).__name__}


def lookups(blob, base, member, count, outlines, flips=()):
    data = bytearray(blob)
    for offset, mask in flips:
        data[offset] ^= mask
    try:
        lookup = reader(bytes(data), base, member, count)
    except Exception as error:
        return {"error": {"type": type(error).__name__, "message": str(error)}}
    results = []
    for outline in outlines:
        results.append(attempt(lambda: lookup(outline)))
    return {"results": results}


def generate_lookup():
    blobs, cases = {}, []
    single_entries = decode_vectors.dictionary(1, 300)
    pair_entries = (decode_vectors.dictionary(2, 260), decode_vectors.dictionary(3, 180))
    blobs["single"] = DictionaryCollectionBuilder(0).build([("Solo", single_entries)])
    blobs["pair"] = DictionaryCollectionBuilder(XIP + 0x30000).build([("First", pair_entries[0]), ("Second", pair_entries[1])])
    special = {"#S": "x", "1": "one", "12": "twelve", "^": "caret", "+": "plus", "^S/+T": "mix", "SKWR-PBLG": "sk", "-Z": "z",
               "#-D": "d", "5": "five", "0": "zero", "1234": "digits", "S*": "star", "AOEU": "vowels", "#STKPWHRAO*EUFRPBLGTSDZ": "all",
               "+T": "plus t", "+STK": "plus stk", "^+S": "caret plus s", "2": "two", "23": "twothree", "3": "three", "34": "threefour",
               "KAT/+T": "two strokes with a plus", "2/3": "two numbers",
               "6": "six", "7": "seven", "8": "eight", "9": "nine", "56": "fivesix", "89": "eightnine", "1/9": "one nine", "4": "four"}
    blobs["special"] = DictionaryCollectionBuilder(0).build([("Special", special)])
    rng = random.Random(5)

    def misses(entries, n):
        absent = []
        while len(absent) < n:
            candidate = decode_vectors.outline(rng, rng.choice((1, 2, 3, 5, 17)))
            if candidate not in entries:
                absent.append(candidate)
        return absent

    invalid = ["", "bad", "ZZZ", "TS", "S//T", "-", "#", "KAT/", "/KAT", "KAT TKOG", "STKPWHRAO*EUFRPBLGTSDZ", "1", "12", "S1", "1S", "^", "+", "#^+S"]
    for label, base, member, count, entries in (("single", 0, 0, 1, single_entries), ("pair", XIP + 0x30000, 0, 2, pair_entries[0]),
                                                ("pair", XIP + 0x30000, 1, 2, pair_entries[1])):
        outlines = list(entries) + misses(entries, 60) + invalid
        cases.append({"name": f"{label} member {member}: every entry, misses and invalid outlines", "blob": label, "base": base,
                      "member": member, "count": count, "flips": [], "outlines": outlines,
                      "expect": lookups(blobs[label], base, member, count, outlines)})
    outlines = list(special) + ["#", "S1", "^^", "+^", "6", "7", "8", "9", "-", "STK", "1/2", "^/^"]
    cases.append({"name": "numbers, modifiers and aliases", "blob": "special", "base": 0, "member": 0, "count": 1, "flips": [],
                  "outlines": outlines, "expect": lookups(blobs["special"], 0, 0, 1, outlines)})
    cases.append({"name": "wrong base", "blob": "single", "base": 4096, "member": 0, "count": 1, "flips": [],
                  "outlines": ["KAT"], "expect": lookups(blobs["single"], 4096, 0, 1, ["KAT"])})
    cases.append({"name": "member out of range", "blob": "pair", "base": XIP + 0x30000, "member": 2, "count": 2, "flips": [],
                  "outlines": ["KAT"], "expect": lookups(blobs["pair"], XIP + 0x30000, 2, 2, ["KAT"])})
    # Damage: header, definition, the stroke table, bitmaps and data.
    keys = list(single_entries)
    probe = keys[:12] + misses(single_entries, 12)
    blob = blobs["single"]
    for offset in list(range(0, 80)) + sorted(rng.sample(range(80, len(blob)), 120)):
        flips = [(offset, 1 << rng.randrange(8))]
        cases.append({"name": f"single: flip byte {offset}", "blob": "single", "base": 0, "member": 0, "count": 1, "flips": [list(f) for f in flips],
                      "outlines": probe, "expect": lookups(blob, 0, 0, 1, probe, flips)})
    return {"blobs": {key: value.hex() for key, value in blobs.items()}, "cases": cases}


def sources_for(name):
    return {"KAT": name, "TKOG": "dog", "KAT/TKOG": name + "!"}


def device_image(overlays, pool_end=SMALL_POOL_END):
    rows = [(identity, name, enabled) for identity, name, enabled, _ in SOURCES] + [(8, "Custom A", True)]
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        for _, name, _, relative in SOURCES:
            if relative:
                path = folder / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(sources_for(name)))
        image, _ = build(folder, collection=(rows, overlays, 9), pool_end=pool_end)
    return bytearray(image)


def replacement(name, tag, extra=None):
    entries = {"KAT": name + " " + tag, "TKOG": "dog", "KAT/TKOG": name + "!", "TKPWAOEG": "added " + tag}
    entries.update(extra or {})
    builder = DictionaryCollectionBuilder(0)
    contents = builder.build([(name, entries)])
    return Image([(name, entries)], contents, builder.pointer_offsets, {name: hashlib.sha256((tag + name).encode()).digest()})


def spec(image, name):
    return {"contents": image.contents.hex(), "pointers": list(image.pointers), "digest": image.digests[name].hex()}


def generate_replace():
    kat, tkog = parse_outline("KAT"), parse_outline("TKOG")
    overlays = {
        (2, kat): (0, b"override of KAT"),               # conflicts: the new base translates KAT differently
        (2, tkog): (0, b"dog"),                          # same translation before and after: no conflict
        (2, parse_outline("KAT/TKOG")): (1, b""),        # a tombstone for an entry whose translation does not change
        (6, kat): (1, b""),                              # a tombstone on a changed entry
        (6, (0x7000,)): (0, b"only local"),              # an entry the base does not have either way
        (3, parse_outline("TKPWAOEG")): (0, "café local".encode()),  # absent before, present after
        (1, (0x10,)): (0, b"personal"), (8, (0x50,)): (0, b"custom"),
    }
    images, cases = {}, []
    base_image = device_image(overlays)
    sources = {"main": (base_image, SMALL_POOL_END)}
    sources["shared"] = (patches.patch_catalog(base_image, patches.share_base("Lapwing Numbers", "Lapwing Commands")), SMALL_POOL_END)
    sources["member"] = (patches.patch_catalog(base_image, patches.patch_row("Lapwing Numbers", {10: 1})), SMALL_POOL_END)
    damaged = bytearray(base_image)
    address, size, crc = base_vectors.catalog_of(damaged)
    probe = Snapshot(lambda a, n: bytes(damaged[a:a + n]), address, size, crc, metadata_only=True, pool_end=SMALL_POOL_END)
    damaged[probe.extents[0][0] * SEGMENT + 20] ^= 1  # the first base image (Lapwing Commands) fails its checksum
    sources["damaged"] = (damaged, SMALL_POOL_END)
    sources["ext253"] = (patches.patch_catalog(base_image, patches.pad_extents(253)), SMALL_POOL_END)
    for label, (data, pool_end) in sources.items():
        images[label] = plan_vectors.record_image(data, pool_end)

    def digest_of(data, name):
        snap = plan_vectors.snapshot_of(data, SMALL_POOL_END)
        row = next(r for r in snap.rows if r[16:16 + r[12]].decode() == name)
        return bytes(row[64:96])

    def case(label, title, names, *, tag="v2", read_original=True, protected=None, same_digest=(), extra=None, wrong=None, pointer_delta=None):
        data, pool_end = sources[label]
        recorder = base_vectors.Recorder(data)
        compiled = {}
        for name in names:
            image = replacement(name, tag, extra)
            if name in same_digest:
                image.digests[name] = digest_of(data, name)
            if pointer_delta is not None:
                contents = bytearray(image.contents)
                struct.pack_into("<I", contents, image.pointers[0], len(contents) + pointer_delta)
                image.contents = bytes(contents)
            compiled[name] = image
        if wrong:
            compiled["Custom A"] = replacement("Custom A", tag)  # not a factory base: never planned
        owned = plan_vectors.owned(data, pool_end) if protected is None else protected
        record = {"kind": "replace", "image": label, "title": title, "readOriginal": read_original, "protected": sorted(owned),
                  "compiled": {name: spec(image, name) for name, image in compiled.items()}}

        def run():
            address, size, crc = base_vectors.catalog_of(data)
            snapshot = Snapshot(recorder, address, size, crc, pool_end=pool_end)
            update, changed, conflicts = plan_replacements(snapshot, owned, compiled, recorder if read_original else None)
            return {"update": None if update is None else {
                        "metadata": update.metadata, "payload": update.payload.hex(),
                        "segments": [[s, len(b), hashlib.sha256(bytes(b)).hexdigest()] for s, b in update.segments.items()]},
                    "changed": changed, "conflicts": conflicts}
        record["expect"] = attempt(run)
        record["reads"] = [{"address": a, "length": n, "hex": d.rstrip(b"\xff").hex()} for a, n, d in recorder.reads]
        record["catalog"] = dict(zip(("address", "bytes", "crc"), base_vectors.catalog_of(data)))
        cases.append(record)

    case("main", "replace one base that has overrides", ["Lapwing Commands"])
    case("main", "replace one base without reading the original", ["Lapwing Commands"], read_original=False)
    case("main", "replace a base with a tombstone conflict", ["Lapwing Base"])
    case("main", "replace a base where an override matches the new one", ["Lapwing Numbers"])
    case("main", "replace several bases", ["Lapwing Commands", "Lapwing Numbers", "Lapwing Base", "Plover Base"])
    case("main", "digest unchanged, nothing to do", ["Lapwing Commands"], same_digest=("Lapwing Commands",))
    case("main", "one changed, one unchanged", ["Lapwing Commands", "Lapwing Numbers"], same_digest=("Lapwing Numbers",))
    case("main", "a base that is not installed", ["Lapwing Commands"], wrong=True)
    case("main", "extra new entries with unicode", ["Lapwing UK Additions"], extra={"S": "café", "TKOG/KAT": "x" * 30})
    case("main", "a relocation pointer at the end of the image", ["Lapwing Commands"], pointer_delta=0)
    case("main", "a relocation pointer past the end", ["Lapwing Commands"], pointer_delta=9)
    case("main", "a relocation pointer just inside the image", ["Lapwing Commands"], pointer_delta=-1, read_original=False)
    case("shared", "rows that share a base", ["Lapwing Commands"])
    case("member", "a row naming a member", ["Lapwing Commands"])
    case("damaged", "installed base fails its checksum", ["Lapwing Commands"])
    case("damaged", "damaged base is skipped without overrides", ["Lapwing Commands"], read_original=False)
    case("ext253", "replace within a 253-extent catalog", ["Lapwing Commands"])
    everything = set(range(SMALL_POOL_END + 1))
    for free, title in (({252, 253, 254}, "only the reserve free"), ({251, 252, 253, 254}, "one segment too few for image and metadata"),
                       ({250, 251, 252, 253, 254}, "exactly enough"), ({2, 3, 4, 252, 253, 254}, "scattered")):
        case("main", f"replace with {title}", ["Lapwing Commands"], protected=everything - free)
        case("main", f"replace two with {title}", ["Lapwing Commands", "Lapwing Base"], protected=everything - free)
    return {"images": images, "cases": cases}


def generate():
    return {"schema": 1, "lookup": generate_lookup(), "replace": generate_replace()}


def main():
    text = json.dumps(generate(), indent=0) + chr(10)
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8").replace(chr(13) + chr(10), chr(10)) != text:
            sys.exit("golden/replace_vectors.json is stale; rerun golden_replace.py")
        print("replace vectors are current")
        return
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline=chr(10))
    data = json.loads(text)
    print(f"wrote {OUT} ({len(text) // 1024} KiB, {len(data['lookup']['cases']) + len(data['replace']['cases'])} cases)")


if __name__ == "__main__":
    main()
