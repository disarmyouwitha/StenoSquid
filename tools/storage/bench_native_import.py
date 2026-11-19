#!/usr/bin/env python3
"""Measure large editable imports through the real native store in strict host NOR.

No hardware is opened. Host time is not nRF52840 latency. The final store is
reopened read-only for validation and every committed entry is checked, including
when a safe capacity refusal stops the import partway through.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/dict_compiler"))
from compile_dict import parse_outline


def dataset(raw, limit):
    source = json.loads(raw)
    if not isinstance(source, dict):
        raise ValueError("Input must be a Plover JSON dictionary")
    entries = {}
    for outline, text in source.items():
        if not isinstance(text, str) or "\0" in text:
            raise ValueError("Translations must be strings without NUL")
        strokes, encoded = parse_outline(outline), text.encode("utf-8")
        if not 1 <= len(strokes) <= 16 or len(encoded) > 2047:
            raise ValueError("Native entry bounds exceeded")
        entries[strokes] = encoded
    selected = list(entries.items())[: limit or None]
    output = bytearray(b"SQBENCH1" + struct.pack("<I", len(selected)))
    for strokes, text in selected:
        output += struct.pack("<BH", len(strokes), len(text))
        output += struct.pack("<" + "I" * len(strokes), *strokes) + text
    return output, len(entries), len(selected)


def fingerprint(directory):
    digest = hashlib.sha256()
    paths = sorted(directory.glob("*.h")) + [
        ROOT / "tools/storage/bench_native_import.cc",
        ROOT / "tools/tests/native_test_nor.h",
    ]
    for path in paths:
        digest.update(path.name.encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument(
        "--image", type=Path, default=ROOT / "build/native-store/image.bin"
    )
    parser.add_argument("--limit", type=int, default=5000, help="0 = entire dictionary")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--storage-headers",
        type=Path,
        default=ROOT / "tools/storage",
        help="Optional archived storage headers for a reproducible comparison",
    )
    args = parser.parse_args()
    if not (args.storage_headers / "native_store.h").is_file():
        parser.error("--storage-headers must contain native_store.h")
    if args.limit < 0:
        parser.error("--limit must be nonnegative")
    raw, image = args.input.read_bytes(), args.image.read_bytes()
    packed, unique, selected = dataset(raw, args.limit)
    if len(image) != 255 * 65536:
        parser.error("--image must be the clean native pool image")
    source_fingerprint = fingerprint(args.storage_headers)
    with tempfile.TemporaryDirectory(prefix="native-import-bench-") as directory:
        temporary = Path(directory)
        data = temporary / "input.bin"
        data.write_bytes(packed)
        binary = temporary / "bench"
        # Quoted includes search the source file's folder before -I paths.
        # Compile a copy here so archived headers really control the experiment.
        cpp = temporary / "bench.cc"
        cpp.write_bytes((ROOT / "tools/storage/bench_native_import.cc").read_bytes())
        subprocess.run(
            [
                *shlex.split(os.environ.get("CXX", "c++")),
                "-std=c++17",
                "-UNDEBUG",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-I",
                str(args.storage_headers.resolve()),
                "-I",
                str(ROOT / "tools/tests"),
                str(cpp),
                "-o",
                str(binary),
            ],
            check=True,
        )
        result = subprocess.run(
            [str(binary), str(args.image.resolve()), str(data)],
            text=True,
            capture_output=False,
            stdout=subprocess.PIPE,
            check=True,
        )
    report = json.loads(result.stdout)
    if fingerprint(args.storage_headers) != source_fingerprint:
        raise RuntimeError(
            "Storage sources changed during the run; repeat the experiment"
        )
    report.update(
        {
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "image_sha256": hashlib.sha256(image).hexdigest(),
            "unique_source_outlines": unique,
            "selected": selected,
            "dataset_sha256": hashlib.sha256(packed).hexdigest(),
            "storage_source_sha256": source_fingerprint,
            "model": "strict NOR, actual NativeStore, 32-range CRC cache; host timing only",
        }
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
