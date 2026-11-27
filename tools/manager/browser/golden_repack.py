"""Golden vectors for repack.js (native_repack: relocate_compact, verify_hints, build_repack, build_merge_repack, validate_pair).

The oracle is Python. Two real recovery bundles are captured from device-shaped images built with the real compiler: one with
journal-only dictionaries and shared edits, one with adjacent compact snapshot dictionaries. Each case records what Python
produces (target image SHA-256, bundle digest, full report) or the exact failure; the JS test must match it byte for byte.
"""
import base64
import copy
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import zlib

import golden_decode as images  # also puts the tools/ directories on sys.path
import golden_plan as plan_vectors
import golden_snapshot as base_vectors
import golden_snapshot_ops as ops_vectors
import native_recovery as recovery
import native_repack as repack
from build_native_image import SEGMENT, SMALL_POOL_END, SOURCES, XIP, build
from compile_dict import parse_outline
from native_update import Snapshot, row_name

OUT = Path(__file__).with_name("golden") / "repack_vectors.json"


def capture_of(image, layout="Mac U.S."):
    address, size, crc = base_vectors.catalog_of(image)
    probe = Snapshot(lambda a, n: bytes(image[a:a + n]), address, size, crc, metadata_only=True, pool_end=SMALL_POOL_END)
    return recovery.capture(lambda a, n: bytes(image[a:a + n]), probe, layout)


def single_image():
    """One journal dictionary holds the only overlay, so compacting it really does save a segment."""
    rows = base_vectors.FACTORY + [(8, "Custom A", True)]
    entries = {(8, tuple(parse_outline(key))): (0, text) for key, text in (("-T", b"alpha"), ("PWOPB", b"beta"), ("KAT/TKOG", b"gamma"),
                                                                            ("STKPWHR", "delta café".encode()))}
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        for _, name, _, relative in SOURCES:
            if relative:
                path = folder / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"KAT": name, "TKOG": "dog", "KAT/TKOG": name + "!"}))
        image, _ = build(folder, collection=(rows, entries, 9), pool_end=SMALL_POOL_END)
    return bytearray(image)


def failure(error):
    return {"type": type(error).__name__, "message": str(error)}


def run_build(bundle, names):
    try:
        image, built, report = repack.build_repack(bundle, names)
        return {"ok": {"image_sha256": hashlib.sha256(image).hexdigest(), "image_length": len(image), "bundle_sha256": built["sha256"],
                       "repack": built["repack"], "extents": len(built["extents"]), "report": report}}
    except Exception as error:
        return {"error": failure(error)}


def run_merge(bundle, upper, lower):
    try:
        original = recovery.reconstruct(bundle)
        built, name = repack.build_merge_repack(bundle, original, upper, lower)
        return {"ok": {"bundle_sha256": built["sha256"], "repack": built["repack"], "name": name, "extents": len(built["extents"])}}
    except Exception as error:
        return {"error": failure(error)}


def generate():
    base_image, snap_image, ids = ops_vectors.build_images()
    big = plan_vectors.big_fixture(SMALL_POOL_END)
    bundles = {"journals": capture_of(base_image), "snapshots": capture_of(snap_image), "big": capture_of(big), "single": capture_of(single_image())}
    cases = []

    for label, bundle in bundles.items():
        for title, names in (("convert everything eligible", None), ("convert the personal dictionary", ["user_dictionary"]), ("convert one journal dictionary", ["Custom A"]), ("convert the other", ["Custom B"]),
                             ("an unknown name", ["Nope"]), ("a factory name", ["Lapwing Base"]), ("a duplicated name", ["Custom A", "Custom A"]),
                             ("no names", [])):
            cases.append({"kind": "build", "bundle": label, "title": f"{label}: {title}", "names": names, "expect": run_build(bundle, names)})

    upper, lower = ids["Snap Two"], ids["Snap One"]
    for title, up, low in (("merge two adjacent snapshots", upper, lower), ("merge in the wrong order", lower, upper), ("merge with a missing id", 9999, lower),
                           ("merge the same snapshot twice", upper, upper)):
        cases.append({"kind": "merge", "bundle": "snapshots", "title": title, "upper": up, "lower": low, "expect": run_merge(bundles["snapshots"], up, low)})
    cases.append({"kind": "merge", "bundle": "journals", "title": "merge without compact dictionaries", "upper": 8, "lower": 9,
                  "expect": run_merge(bundles["journals"], 8, 9)})

    # validate_pair on damaged optimized bundles: change the repack metadata or the layout and sign the result again.
    def tweaks(label, names):
        source = bundles[label]
        image, built, _ = repack.build_repack(source, names)
        original = recovery.reconstruct(source)

        def tweak(title, change):
            bundle = copy.deepcopy(built)
            change(bundle)
            bundle["sha256"] = recovery.digest(bundle)
            try:
                repacked = recovery.reconstruct(bundle)
                report = repack.validate_pair(source, bundle, original, repacked)
                outcome = {"ok": hashlib.sha256(json.dumps(report, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
            except Exception as error:
                outcome = {"error": failure(error)}
            cases.append({"kind": "pair", "title": f"{label}: {title}", "bundle": label, "names": names, "changes": title, "expect": outcome})

        tweak("an unmodified optimized bundle", lambda b: None)
        tweak("a different source bundle hash", lambda b: b["repack"].update(source_bundle_sha256="0" * 64))
        tweak("a different target image hash", lambda b: b["repack"].update(target_image_sha256="0" * 64))
        tweak("another host layout", lambda b: b.update(host_layout="Windows U.S. (plain text)"))
        tweak("no repack section", lambda b: b.pop("repack"))
        tweak("conversion list is not a list", lambda b: b["repack"].update(converted="Custom A"))
        tweak("conversion list has a duplicate", lambda b: b["repack"].update(converted=["Custom A", "Custom A"]))
        tweak("conversion list has a number", lambda b: b["repack"].update(converted=[1]))
        tweak("conversion of a factory dictionary", lambda b: b["repack"].update(converted=["Lapwing Base"]))
        tweak("conversion list claims fewer", lambda b: b["repack"].update(converted=[]))
        tweak("conversion list claims more", lambda b: b["repack"].update(converted=["user_dictionary", "Custom A"]))
        tweak("merge list with a conversion", lambda b: b["repack"].update(merged=[8, 9]))
        tweak("a merge pair of the wrong length", lambda b: (b["repack"].update(converted=[], merged=[8])))
        tweak("a merge pair with a non-number", lambda b: (b["repack"].update(converted=[], merged=["a", 9])))

    tweaks("journals", None)
    tweaks("big", ["user_dictionary"])
    tweaks("single", ["Custom A"])

    # Damaged compact blobs through relocate_compact and verify_hints.
    address, size, crc = base_vectors.catalog_of(base_image)
    probe = Snapshot(lambda a, n: bytes(base_image[a:a + n]), address, size, crc, metadata_only=True, pool_end=SMALL_POOL_END)
    start, span, length, *_ = [extent for extent in probe.extents if extent[4] == 1][1]
    blob = bytes(base_image[start * SEGMENT:start * SEGMENT + length])
    old_base = XIP + start * SEGMENT
    cases.append({"kind": "blob", "title": "the blob used by the compact cases", "hex": blob.hex(), "old_base": old_base})

    def relocate(title, data, old, new):
        pointers = []
        try:
            out = repack.relocate_compact(data, old, new, pointers)
            outcome = {"ok": {"sha256": hashlib.sha256(out).hexdigest(), "pointers": pointers}}
        except Exception as error:
            outcome = {"error": failure(error)}
        cases.append({"kind": "relocate", "title": title, "hex": data.hex(), "old_base": old, "new_base": new, "expect": outcome})

    def hints(title, data, base):
        try:
            repack.verify_hints(data, base)
            outcome = {"ok": True}
        except Exception as error:
            outcome = {"error": failure(error)}
        cases.append({"kind": "hints", "title": title, "hex": data.hex(), "base": base, "expect": outcome})

    def flip(data, at, xor=1):
        out = bytearray(data)
        out[at] ^= xor
        return bytes(out)

    relocate("relocate to a new base", blob, old_base, XIP + 40 * SEGMENT)
    relocate("relocate to the same base", blob, old_base, old_base)
    relocate("a pointer outside the extent", blob, old_base + 4, XIP + 40 * SEGMENT)
    relocate("a base too high", blob, old_base + SEGMENT * 20, XIP)
    relocate("too short", blob[:39], old_base, XIP)
    relocate("a broken magic", flip(blob, 0), old_base, XIP)
    relocate("reserved bytes set", flip(blob, 6), old_base, XIP)
    relocate("later reserved bytes set", flip(blob, 20), old_base, XIP)
    relocate("a member count of zero", blob[:4] + b"\0\0" + blob[6:], old_base, XIP)
    relocate("too many members", blob[:4] + struct.pack("<H", 65) + blob[6:], old_base, XIP)
    relocate("a text pointer that is misaligned in the definition", flip(blob, 36, 2), old_base, XIP)
    relocate("a damaged definition", flip(blob, struct.unpack_from("<I", blob, 36)[0] - old_base + 1, 0x20), old_base, XIP)
    hints("intact hints", blob, old_base)
    hints("a broken hint directory", flip(blob, len(blob) - 12), old_base)
    hints("a hint count that is too big", flip(blob, len(blob) - 20, 0x40), old_base)
    hints("a hint text offset that is out of range", flip(blob, len(blob) - 20 - 8 * 3, 0x7f), old_base)
    hints("a damaged hint stroke", flip(blob, len(blob) - 20 - 4, 0x01), old_base)
    hints("a damaged translation", flip(blob, struct.unpack_from("<I", blob, 12)[0] - old_base + 1, 0x01), old_base)
    hints("the wrong base", blob, old_base + 4)
    return {"schema": 1, "bundles": bundles, "cases": cases}


def main():
    text = json.dumps(generate(), indent=0) + chr(10)
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8").replace(chr(13) + chr(10), chr(10)) != text:
            sys.exit("golden/repack_vectors.json is stale; rerun golden_repack.py")
        print("repack vectors are current")
        return
    OUT.write_text(text, encoding="utf-8", newline=chr(10))
    print(f"wrote {OUT} ({len(text) // 1024} KiB, {len(json.loads(text)['cases'])} cases)")


if __name__ == "__main__":
    main()
