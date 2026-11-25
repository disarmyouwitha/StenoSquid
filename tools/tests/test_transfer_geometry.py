#!/usr/bin/env python3
"""Transfer session on a 64 MiB pool, above the old 16 MiB boundary (needs the Phase 1 geometry work)."""
import json
from pathlib import Path
import subprocess
import tempfile

from hostlib import compiler
from test_native_stage import ROOT, write_plan
from build_native_image import LARGE_POOL_END, SEGMENT, SOURCES, build, catalog_header
from native_snapshot import compile_snapshot, plan_named_snapshot
from native_update import Snapshot, plan
import struct


def view(image):
    size, crc = struct.unpack_from("<II", image, SEGMENT + 16)
    return Snapshot(lambda at, n: bytes(image[at:at + n]), SEGMENT + 32, size, crc,
                    strict=True, pool_end=LARGE_POOL_END)


def main():
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        for _, name, _, relative in SOURCES:
            if relative:
                path = folder / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"KAT": name}))
        original, _ = build(folder, pool_end=LARGE_POOL_END)
        baseline = view(original)
        named = plan_named_snapshot(baseline, set(range(512)), "High image",
                                    compile_snapshot("High image", {(8,): b"high compact"}))
        image = bytearray(original)
        for block, data in named.segments.items():
            image[block * SEGMENT:block * SEGMENT + len(data)] = data
        image[SEGMENT:SEGMENT + 32 + len(named.payload)] = catalog_header(4, named.payload) + named.payload
        journal = plan(view(image), set(range(768)), {(8,): b"high journal"})
        files = [folder / name for name in ("initial.bin", "named.plan", "journal.plan", "geometry")]
        files[0].write_bytes(original)
        write_plan(files[1], named)
        write_plan(files[2], journal)
        subprocess.run([*compiler(), "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-I", str(ROOT / "tools/storage"), str(ROOT / "tools/tests/test_transfer_geometry.cc"),
                        "-o", str(files[3])], check=True)
        subprocess.run([str(files[3]), *map(str, files[:3])], check=True)


if __name__ == "__main__":
    main()
