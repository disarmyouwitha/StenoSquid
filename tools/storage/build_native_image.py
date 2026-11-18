#!/usr/bin/env python3
"""Build a clean Candidate-B shared-pool image from the pinned JSON sources.

This is an explicit fresh provision, not a migration or an in-place base update.
Only --output files are written; use native_install.py to provision a test board.
"""

import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
import zlib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/dict_compiler"))
from compile_dict import DictionaryCollectionBuilder

SEGMENT = 65536
LIMIT = 255 * SEGMENT
XIP = 0x12000000
SOURCES = [
    (1, "user_dictionary", True, None),
    (2, "Lapwing Commands", True, "extras/lapwing-commands.json"),
    (3, "Lapwing Numbers", True, "extras/lapwing-numbers.json"),
    (4, "Lapwing UK Additions", False, "extras/lapwing-uk-additions.json"),
    (5, "Lapwing Proper Nouns", True, "extras/lapwing-proper-nouns.json"),
    (6, "Lapwing Base", True, "lapwing/lapwing-base.json"),
    (7, "Plover Base", False, "plover/plover-main.json"),
]


def catalog_header(generation, payload):
    header = struct.pack(
        "<8sQII", b"SQBCAT02", generation, len(payload), zlib.crc32(payload)
    )
    return header + struct.pack("<II", zlib.crc32(header), 0x434F4D54)


def build(source_root):
    image = bytearray(b"\xff" * LIMIT)
    journal = bytearray(32)
    journal[:8] = b"SQJSEG01"
    struct.pack_into("<Q", journal, 8, 1)
    struct.pack_into("<I", journal, 16, zlib.crc32(journal[:16]))
    image[2 * SEGMENT : 2 * SEGMENT + 32] = journal
    extents = [struct.pack("<HHIIIQ", 2, 1, SEGMENT, 0, 2, 1)]
    dictionaries = []
    manifest = []
    next_segment = 3
    for identity, name, enabled, source in SOURCES:
        base = 65535
        digest = b"\0" * 32
        if source:
            raw = (source_root / source).read_bytes()
            entries = json.loads(raw)
            address = next_segment * SEGMENT
            binary = DictionaryCollectionBuilder(XIP + address).build([(name, entries)])
            count = (len(binary) + SEGMENT - 1) // SEGMENT
            if next_segment + count > 252:
                raise ValueError("Base images exhaust the three-segment reserve")
            image[address : address + len(binary)] = binary
            base = len(extents)
            digest = hashlib.sha256(raw).digest()
            extents.append(
                struct.pack(
                    "<HHIIIQ",
                    next_segment,
                    count,
                    len(binary),
                    zlib.crc32(binary),
                    1,
                    0,
                )
            )
            manifest.append(
                dict(
                    id=identity,
                    name=name,
                    address=address,
                    bytes=len(binary),
                    segments=count,
                    crc32=zlib.crc32(binary),
                    source_sha256=digest.hex(),
                )
            )
            next_segment += count
        encoded = name.encode()
        row = bytearray(96)
        struct.pack_into("<IIH", row, 0, identity, int(enabled), base)
        row[12] = len(encoded)
        row[16 : 16 + len(encoded)] = encoded
        row[64:96] = digest
        dictionaries.append(row)
    prefix = bytearray(32)
    struct.pack_into("<IHHQ", prefix, 0, 8, len(dictionaries), len(extents), 0)
    payload = bytes(prefix) + b"".join(dictionaries) + b"".join(extents)
    for slot in range(2):
        blob = catalog_header(slot + 1, payload) + payload
        image[slot * SEGMENT : slot * SEGMENT + len(blob)] = blob
    report = dict(
        schema=1,
        format="SQBCAT02",
        bytes=LIMIT,
        crc32=zlib.crc32(image),
        sha256=hashlib.sha256(image).hexdigest(),
        catalog_bytes=len(payload),
        base_images=manifest,
        free_segments=255 - next_segment,
        provision="explicit clean erase; no migration",
        personal_id=1,
    )
    return bytes(image), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=ROOT / "build")
    parser.add_argument(
        "--output", type=Path, default=ROOT / "build/native-store/image.bin"
    )
    args = parser.parse_args()
    image, report = build(args.sources)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(image)
    args.output.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        f"Built {len(report['base_images'])} bases; {report['free_segments']} shared segments free"
    )


if __name__ == "__main__":
    main()
