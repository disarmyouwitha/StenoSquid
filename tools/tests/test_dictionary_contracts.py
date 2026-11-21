#!/usr/bin/env python3
"""Verify pinned source hashes and the individual factory image layout."""
import hashlib
from pathlib import Path
import struct
import sys
import zlib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/dictionaries"))
sys.path.insert(0, str(ROOT / "tools/storage"))
from build_native_image import SOURCES, build
from manifest import BASES


def main():
    hashes = {f"{base['directory']}/{file}": digest
              for base in BASES.values() for _, file, _, digest in base["sources"]}
    image, report = build(ROOT / "build")
    assert [base["name"] for base in report["base_images"]] == [row[1] for row in SOURCES[1:]]
    for (_, _, _, source), base in zip(SOURCES[1:], report["base_images"], strict=True):
        assert len(base["dictionaries"]) == 1
        assert base["dictionaries"][0]["source_sha256"] == hashes[source]
        assert hashlib.sha256((ROOT / "build" / source).read_bytes()).hexdigest() == hashes[source]
        blob = image[base["address"]:base["address"] + base["bytes"]]
        assert struct.unpack_from("<4sI", blob) == (b"JSC4", 1)
        assert zlib.crc32(blob) == base["crc32"]
    print("PASS: six individually compiled images with pinned sources and verified CRCs")


if __name__ == "__main__":
    main()
