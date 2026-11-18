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
from jsc4 import reader
from native_install import check_image


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
    verified = 0
    for base in report["base_images"]:
        identity = base["id"]
        source = next(path for ident, _, _, path in SOURCES if ident == identity)
        raw = (sources / source).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == base["source_sha256"]
        binary = image[base["address"] : base["address"] + base["bytes"]]
        assert zlib.crc32(binary) == base["crc32"]
        lookup = reader(binary, XIP + base["address"])
        for outline, translation in json.loads(raw).items():
            assert lookup(outline) == translation, (base["name"], outline)
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
                    json.dumps({"KAT": "cat", "HE/HROE": "hello", "SA": "ça va"})
                )
        image, report = build(path)
        assert validate(image, report, path) == 18
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
        subprocess.run([str(binary), str(image_path)], check=True)
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
