#!/usr/bin/env python3
"""Validate native clean images, pointer rebasing and the real C++ installer."""

import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import zlib
from hostlib import compiler

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/storage"))
sys.path.insert(0, str(ROOT / "tools/dict_compiler"))
from build_native_image import build, SOURCES, SEGMENT, XIP
from compile_dict import parse_outline
from jsc4 import reader
from native_install import check_image
from native_update import Snapshot, row_name


def validate(image, report, sources):
    check_image(image, report)
    for slot in range(2):
        magic, generation, count, crc, hcrc, commit = struct.unpack_from(
            "<8sQIIII", image, slot * SEGMENT
        )
        assert magic == b"SQBCAT02" and generation == slot + 1 and commit == 0x434F4D54
        assert zlib.crc32(image[slot * SEGMENT : slot * SEGMENT + 24]) == hcrc
        assert (
            zlib.crc32(image[slot * SEGMENT + 32 : slot * SEGMENT + 32 + count]) == crc
        )
    assert report["base_images"][0]["address"] == 2 * SEGMENT
    first, second = report["base_images"]
    assert second["address"] == first["address"] + first["segments"] * SEGMENT
    initial_journal = second["address"] + second["segments"] * SEGMENT
    assert image[initial_journal:initial_journal + 8] == b"SQJSEG01"
    verified = 0
    for base in report["base_images"]:
        binary = image[base["address"] : base["address"] + base["bytes"]]
        assert zlib.crc32(binary) == base["crc32"]
        for member in base["dictionaries"]:
            identity = member["id"]
            source = next(path for ident, _, _, path in SOURCES if ident == identity)
            raw = (sources / source).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == member["source_sha256"]
            row_offset = SEGMENT + 64 + 96 * (identity - 1)
            extent, index = struct.unpack_from("<HB", image, row_offset + 8)
            start = struct.unpack_from("<H", image, SEGMENT + 64 + 96 * 7 + 24 * extent)[0]
            assert start * SEGMENT == base["address"] and index == member["index"]
            lookup = reader(binary, XIP + base["address"], index, len(base["dictionaries"]))
            for outline, translation in json.loads(raw).items():
                assert lookup(outline) == translation, (member["name"], outline)
                verified += 1
    return verified


def main():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        for _, _, _, source in SOURCES:
            if source:
                target = path / source
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    json.dumps({"KAT": target.stem, "HE/HROE": "hello", "SA": "ça va"})
                )
        personal = {parse_outline("KAT"): "restored ç".encode(), parse_outline("HE/HROE"): b""}
        # Span multiple journals so decoding exercises epoch/cursor recovery.
        for i in range(1, 301):
            personal[(0x1000 + i, 4)] = (f"é{i} " * 70).encode()
        image, report = build(path, personal)
        assert report["personal_entries"] == len(personal)
        assert validate(image, report, path) == 18
        rows = [(8, "Medical", True)] + [(10 if identity == 1 else identity, name, enabled and identity != 2)
                                          for identity, name, enabled, _ in SOURCES]
        entries = {(8, (1,)): (0, b"named"), (10, (1,)): (0, "ça".encode()), (2, (2,)): (1, b"")}
        restored, manifest = build(path, collection=(rows, entries, 12))
        root = SEGMENT + 32
        snapshot = Snapshot(lambda at, n: restored[at:at+n], root, manifest["catalog_bytes"],
                            struct.unpack_from("<I", restored, root - 12)[0])
        assert [row_name(row) for row in snapshot.rows] == [name for _, name, _ in rows]
        assert snapshot.personal == 10 and snapshot.next_id == 12
        assert snapshot.entries[8, (1,)].text == b"named"
        assert snapshot.entries[10, (1,)].text == "ça".encode()
        assert snapshot.entries[2, (2,)].operation == 1
        restored_path = path / "restored.bin"
        restored_path.write_bytes(restored)
        image_path = path / "native.bin"
        image_path.write_bytes(image)
        binary = path / "native-image"
        subprocess.run(
            [
                *compiler(),
                "-std=c++17",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-I",
                str(ROOT / "tools/storage"),
                str(ROOT / "tools/tests/test_native_image.cc"),
                "-o",
                str(binary),
            ],
            check=True,
        )
        subprocess.run([str(binary), str(image_path), str(restored_path)], check=True)
    if all(
        not source or (ROOT / "build" / source).exists() for _, _, _, source in SOURCES
    ):
        image, report = build(ROOT / "build")
        verified = validate(image, report, ROOT / "build")
        print(
            f"Native pinned images: {verified:,} exact translations after rebasing; {report['free_segments']} shared segments free"
        )


if __name__ == "__main__":
    main()
