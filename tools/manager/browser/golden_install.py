"""Golden vectors for install.js: build_native_image.build (installer path), native_install.check_image and provision.

The oracle is Python's own functions. Images are built from small synthetic pinned sources (the same files go to the JS test), and
the vectors record each image's SHA-256, CRC-32 and report, how check_image judges deliberately damaged copies, and the console
transcript of provision() against a scripted keyboard.
"""
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import zlib

import golden_snapshot as base_vectors  # also puts the tools/ directories on sys.path
from build_native_image import LARGE_POOL_END, SEGMENT, SMALL_POOL_END, SOURCES, build
from native_install import check_image, provision

OUT = Path(__file__).with_name("golden") / "install_vectors.json"


def synthetic_sources():
    files = {}
    for _, name, _, relative in SOURCES:
        if relative:
            files[relative] = json.dumps({"KAT": name, "TKOG": "dog", "KAT/TKOG": name + "!", "STKPWHR": "café " + name,
                                          "PWOPB": "ball", "-T": "the"}, ensure_ascii=name == "Lapwing Base")
    return files


def build_with(files, packages, pool_end=SMALL_POOL_END):
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        for relative, text in files.items():
            (folder / relative).parent.mkdir(parents=True, exist_ok=True)
            (folder / relative).write_text(text, encoding="utf-8")
        return build(folder, None, packages=packages, pool_end=pool_end)


def reseal(image):
    """Recompute both catalogs' checksums so a deliberate row-level change reaches the deeper checks."""
    for root in (0, SEGMENT):
        size = struct.unpack_from("<I", image, root + 16)[0]
        struct.pack_into("<I", image, root + 20, zlib.crc32(bytes(image[root + 32:root + 32 + size])))
        struct.pack_into("<I", image, root + 24, zlib.crc32(bytes(image[root:root + 24])))


def manifest_of(image, **changes):
    return {"format": "SQBCAT02", "crc32": zlib.crc32(bytes(image)), "sha256": hashlib.sha256(bytes(image)).hexdigest(), **changes}


def judge(image, manifest):
    try:
        return {"absent": sorted(check_image(bytes(image), manifest))}
    except Exception as error:
        return {"error": {"type": type(error).__name__, "message": str(error)}}


class Console:
    def __init__(self, replies):
        self.replies, self.log, self.counts = replies, [], {}

    def command(self, text, timeout=30):
        words = text.split()
        verb = words[1] if text.startswith("native_install ") and words[1] != "update" else text
        ordinal = self.counts[verb] = self.counts.get(verb, 0) + 1
        reply = self.replies.get((verb, ordinal), self.replies.get((verb, 0)))
        if reply is None:
            reply = {"native_install status": "NATIVE2 UNPROVISIONED 0", "native_install geometry": f"OK POOL1 {SMALL_POOL_END}",
                     "native_install update factory-member-capabilities": "OK FACTORY2"}.get(text, "OK")
        self.log.append({"c": text, "t": timeout, "r": reply})
        return reply


def generate():
    files = synthetic_sources()
    cases, checks, provisions = [], [], []
    for title, packages, pool_end in (("all factory dictionaries", None, SMALL_POOL_END), ("plover only", ["plover"], SMALL_POOL_END),
                                      ("lapwing base only", ["lapwing-base"], SMALL_POOL_END),
                                      ("commands and plover", ["lapwing-commands", "plover"], SMALL_POOL_END),
                                      ("numbers only", ["lapwing-numbers"], SMALL_POOL_END), ("all, large pool", None, LARGE_POOL_END),
                                      ("no packages", [], SMALL_POOL_END), ("unknown package", ["nothing"], SMALL_POOL_END),
                                      ("bad geometry", None, 300)):
        record = {"title": title, "packages": packages, "pool_end": pool_end}
        try:
            image, report = build_with(files, packages, pool_end)
            record["expect"] = {"sha256": hashlib.sha256(image).hexdigest(), "length": len(image), "report": report}
        except Exception as error:
            record["expect"] = {"error": {"type": type(error).__name__, "message": str(error)}}
        cases.append(record)

    image, report = build_with(files, None)
    plover_only, _ = build_with(files, ["plover"])

    def damaged(title, source, edits, *, seal=True, fix_manifest=True, manifest=None):
        data = bytearray(source)
        for at, xor in edits:
            data[at] ^= xor
        if seal:
            reseal(data)
        shown = manifest(data) if manifest else (manifest_of(data) if fix_manifest else manifest_of(source))
        checks.append({"title": title, "source": "plover" if source is plover_only else "all", "edits": edits, "seal": seal,
                       "manifest": "fixed" if fix_manifest else "stale", "format": shown["format"], "expect": judge(data, shown)})

    # offsets inside the first catalog's row table: header 32 bytes, then 96-byte rows (identity at +0, marker at +11, name length at +12)
    row = lambda index, field: 32 + 32 + index * 96 + field  # catalog payload starts after the 32-byte header, rows after 32 bytes of prefix
    damaged("an intact image", image, [])
    damaged("an intact image with a package left out", plover_only, [])
    damaged("a stale manifest", image, [(SEGMENT * 3 + 10, 1)], seal=False, fix_manifest=False)
    damaged("the wrong format in the manifest", image, [], manifest=lambda data: manifest_of(data, format="SQBCAT01"))
    damaged("a catalog with a broken magic", image, [(0, 1)])
    damaged("a catalog payload that fails its checksum", image, [(row(2, 20), 1)], seal=False)
    damaged("the second catalog payload fails its checksum", image, [(SEGMENT + row(2, 20), 1)], seal=False)
    damaged("a factory image without its JSC4 header", image, [(next(i for i in range(2 * SEGMENT, len(image)) if image[i:i + 4] == b"JSC4"), 1)])
    damaged("a factory row marked as an experimental snapshot", image, [(row(1, 11), 1), (SEGMENT + row(1, 11), 1)])
    damaged("a factory row with a member index", image, [(row(1, 10), 1), (SEGMENT + row(1, 10), 1)])
    damaged("a factory row pointing at the wrong extent", image, [(row(2, 8), 2), (SEGMENT + row(2, 8), 2)])
    damaged("two factory rows sharing an image", image, [(row(2, 8), 1), (SEGMENT + row(2, 8), 1)])
    damaged("an uninstalled row that is enabled", plover_only, [(row(1, 4), 1), (SEGMENT + row(1, 4), 1)])
    digest = [(row(1, 64 + i), plover_only[row(1, 64 + i)]) for i in range(32)]
    damaged("an uninstalled row without its source digest", plover_only, digest + [(SEGMENT + at, xor) for at, xor in digest])
    damaged("an uninstalled row with another name", plover_only, [(row(1, 16), 1), (SEGMENT + row(1, 16), 1)])
    damaged("a journal that is out of order", image, [(next(i for i in range(2 * SEGMENT, len(image)) if image[i:i + 8] == b"SQJSEG01") + 8, 4)])

    for title, packages, replies in (
            ("a clean install", None, {}), ("installing plover only", ["plover"], {}),
            ("a large pool", None, {("geometry", 0): f"OK POOL1 {LARGE_POOL_END}"}),
            ("an older keyboard without geometry", None, {("geometry", 0): "ERR Command unavailable on this keyboard"}),
            ("no active installation", None, {("geometry", 0): "ERR No native installation active"}),
            ("an unknown geometry", None, {("geometry", 0): "OK POOL1 512"}),
            ("old firmware", None, {("native_install update factory-member-capabilities", 0): "OK FACTORY1"}),
            ("not a native keyboard", None, {("status", 0): "ERR nope"}),
            ("another status than expected", None, {("status", 0): "NATIVE2 READY 12"}),
            ("begin refused", None, {("begin", 0): "ERR busy"}),
            ("an erase refused", None, {("erase", 7): "ERR flash"}),
            ("a write refused", None, {("write", 3): "ERR flash"}),
            ("a write refused and the abort refused too", None, {("write", 3): "ERR flash", ("abort", 0): "ERR no"}),
            ("finish refused", None, {("finish", 0): "ERR verify failed"})):
        data, report = build_with(files, packages)
        console, messages = Console(replies), []
        expected_status = "NATIVE2 UNPROVISIONED 0" if title in ("another status than expected", "a clean install") else None
        try:
            provision(console, data, report, messages.append, expected_status=expected_status)
            outcome = {"done": True}
        except Exception as error:
            outcome = {"error": {"type": type(error).__name__, "message": str(error)}}
        provisions.append({"title": title, "packages": packages, "expected_status": expected_status,
                           "replies": [[verb, ordinal, reply] for (verb, ordinal), reply in sorted(replies.items())],
                           "expect": outcome, "progress": messages, "log": console.log})
    # an image that does not fit the pool, and one that must be padded
    big, big_report = build_with(files, None, LARGE_POOL_END)
    console, messages = Console({}), []
    try:
        provision(console, big, big_report, messages.append)
        outcome = {"done": True}
    except Exception as error:
        outcome = {"error": {"type": type(error).__name__, "message": str(error)}}
    provisions.append({"title": "a large image on a small keyboard", "packages": "large", "expected_status": None, "replies": [],
                       "expect": outcome, "progress": messages, "log": console.log})
    return {"schema": 1, "sources": files, "builds": cases, "checks": checks, "provisions": provisions}


def main():
    text = json.dumps(generate(), indent=0) + chr(10)
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8").replace(chr(13) + chr(10), chr(10)) != text:
            sys.exit("golden/install_vectors.json is stale; rerun golden_install.py")
        print("install vectors are current")
        return
    OUT.write_text(text, encoding="utf-8", newline=chr(10))
    document = json.loads(text)
    print(f"wrote {OUT} ({len(text) // 1024} KiB, {len(document['builds'])} builds, {len(document['checks'])} checks, {len(document['provisions'])} provisions)")


if __name__ == "__main__":
    main()
