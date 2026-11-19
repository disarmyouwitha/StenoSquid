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


def write_journal_header(image, segment, epoch, *, extent_segment=None):
    prefix = struct.pack("<8sQ", b"SQJSEG01", epoch)
    header = prefix + struct.pack("<I", zlib.crc32(prefix)) + bytes(12)
    address = segment * SEGMENT
    image[address : address + len(header)] = header
    return struct.pack("<HHIIIQ", segment if extent_segment is None else extent_segment,
                       1, SEGMENT, 0, 2, epoch)


def overlay_record_span(strokes, text):
    return (43 + 4 * len(strokes) + len(text)) & ~3


def seed_entries(image, extents, next_segment, entries):
    """Build both indexes for a validated overlay collection before USB writes."""
    if not entries:
        return next_segment
    epoch, segment = 1, 2
    address = segment * SEGMENT + 256
    records = {}
    for sequence, ((identity, strokes), (operation, text)) in enumerate(entries.items(), 1):
        payload = struct.pack("<IBBH", identity, operation, len(strokes), len(text))
        payload += struct.pack(f"<{len(strokes)}I", *strokes) + text
        span = overlay_record_span(strokes, text)
        if address + span > (segment + 1) * SEGMENT:
            segment, next_segment = next_segment, next_segment + 1
            if next_segment > 252:
                raise ValueError("Saved dictionary entries exhaust the three-segment reserve")
            epoch += 1
            extents.append(write_journal_header(image, segment, epoch))
            address = segment * SEGMENT + 256
        crc = zlib.crc32(payload, zlib.crc32(struct.pack("<Q", sequence)))
        header = struct.pack("<4sIQII", b"SQJR", span, sequence, len(payload), crc)
        record = header + struct.pack("<II", zlib.crc32(header), 0x434F4D54) + payload
        image[address : address + len(record)] = record
        records[identity, strokes] = address
        address += span
    reverse = sorted((key for key, (operation, _) in entries.items() if not operation),
                     key=lambda key: (entries[key][1], key))
    for kind, order in ((3, sorted(entries)), (4, reverse)):
        if not order:
            continue
        index = b"".join(struct.pack("<I", records[key]) for key in order)
        count = (len(index) + SEGMENT - 1) // SEGMENT
        if next_segment + count > 252:
            raise ValueError("Saved dictionary indexes exhaust the three-segment reserve")
        start = next_segment * SEGMENT
        image[start : start + len(index)] = index
        extents.append(struct.pack("<HHIIIQ", next_segment, count, len(index),
                                   zlib.crc32(index), kind, 0))
        next_segment += count
    return next_segment


def read_personal(path):
    sys.path.insert(0, str(ROOT / "tools/dictionaries"))
    from import_personal import parse_named

    return parse_named(path.read_text(encoding="utf-8"))[0]


def build(source_root, personal=None, *, collection=None):
    image = bytearray(b"\xff" * LIMIT)
    extents = [write_journal_header(image, 2, 1)]
    dictionaries = []
    manifest = []
    next_segment = 3
    location = {}
    for region_name, sources in (("Lapwing + extras", SOURCES[1:6]), ("Plover Base", SOURCES[6:])):
        members = []
        definitions = []
        for index, (identity, name, _, source) in enumerate(sources):
            raw = (source_root / source).read_bytes()
            digest = hashlib.sha256(raw).digest()
            definitions.append((name, json.loads(raw)))
            location[name] = len(extents), index, digest
            members.append(dict(id=identity, index=index, name=name, source_sha256=digest.hex()))
        address = next_segment * SEGMENT
        binary = DictionaryCollectionBuilder(XIP + address).build(definitions)
        count = (len(binary) + SEGMENT - 1) // SEGMENT
        if next_segment + count > 252:
            raise ValueError("Base images exhaust the three-segment reserve")
        image[address : address + len(binary)] = binary
        crc = zlib.crc32(binary)
        extents.append(struct.pack("<HHIIIQ", next_segment, count, len(binary), crc, 1, 0))
        manifest.append(dict(name=region_name, address=address, bytes=len(binary),
                             segments=count, crc32=crc, dictionaries=members))
        next_segment += count
    if collection is None:
        rows = [(identity, name, enabled) for identity, name, enabled, _ in SOURCES]
        entries = {(1, strokes): (0, text) for strokes, text in (personal or {}).items()}
        next_id = 8
    else:
        rows, entries, next_id = collection
    personal_id = next(identity for identity, name, _ in rows if name == "user_dictionary")
    for identity, name, enabled in rows:
        base, index, digest = location.get(name, (65535, 0, bytes(32)))
        encoded = name.encode()
        row = bytearray(96)
        struct.pack_into("<IIHB", row, 0, identity, int(enabled), base, index)
        row[12] = len(encoded)
        row[16 : 16 + len(encoded)] = encoded
        row[64:96] = digest
        dictionaries.append(row)
    next_segment = seed_entries(image, extents, next_segment, entries)
    prefix = bytearray(32)
    struct.pack_into("<IHHQ", prefix, 0, next_id, len(dictionaries), len(extents), len(entries))
    payload = bytes(prefix) + b"".join(dictionaries) + b"".join(extents)
    for slot in range(2):
        blob = catalog_header(slot + 1, payload) + payload
        image[slot * SEGMENT : slot * SEGMENT + len(blob)] = blob
    report = dict(
        schema=2,
        format="SQBCAT02",
        bytes=LIMIT,
        crc32=zlib.crc32(image),
        sha256=hashlib.sha256(image).hexdigest(),
        catalog_bytes=len(payload),
        base_images=manifest,
        free_segments=255 - next_segment,
        provision="explicit clean erase; no migration",
        personal_id=personal_id,
        personal_entries=sum(identity == personal_id and op == 0
                             for (identity, _), (op, _) in entries.items()),
    )
    return bytes(image), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=ROOT / "build")
    parser.add_argument(
        "--output", type=Path, default=ROOT / "build/native-store/image.bin"
    )
    parser.add_argument("--personal", type=Path, metavar="BACKUP.JSON")
    args = parser.parse_args()
    image, report = build(args.sources, read_personal(args.personal) if args.personal else None)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(image)
    args.output.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        f"Built {len(report['base_images'])} bases; {report['free_segments']} shared segments free"
    )


if __name__ == "__main__":
    main()
