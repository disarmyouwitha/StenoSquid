#!/usr/bin/env python3
"""Validate a large-pool catalog, high-address edits and compatible host boundaries."""
import base64
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import zlib

from hostlib import compiler
from test_native_stage import ROOT, write_plan
from build_native_image import LARGE_POOL_END, SEGMENT, SOURCES, build, catalog_header
from native_install import begin_for_pool, check_image, provision
from native_layout import read_layout
from native_recovery import capture, reconstruct
from native_snapshot import compile_snapshot, plan_named_snapshot
from native_update import Snapshot, effective_entries, plan, read_snapshot


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
        original, manifest = build(folder, pool_end=LARGE_POOL_END)
        check_image(original, manifest)
        baseline = view(original)

        class WrongCapacity:
            def command(self, command):
                replies = {
                    "native_install update factory-member-capabilities": "OK FACTORY2",
                    "native_install status": "NATIVE2 UNPROVISIONED 0",
                    "native_install geometry": "OK POOL1 255",
                }
                return replies[command]

        try:
            provision(WrongCapacity(), original, manifest)
        except ValueError as error:
            assert "larger than" in str(error)
        else:
            raise AssertionError("Accepted a larger image on 16 MiB")
        assert begin_for_pool("begin CLEAR 123", LARGE_POOL_END) == "large 1023 begin CLEAR 123"

        named = plan_named_snapshot(baseline, set(range(512)), "High image",
                                    compile_snapshot("High image", {(8,): b"high compact"}))
        assert min(named.segments) == 512
        image = bytearray(original)
        # Simulate the previously successful catalog update for the journal planner.
        for block, data in named.segments.items():
            image[block * SEGMENT:block * SEGMENT + len(data)] = data
        image[SEGMENT:SEGMENT + 32 + len(named.payload)] = catalog_header(4, named.payload) + named.payload
        current = view(image)
        assert effective_entries(current, "High image", lambda at, n: image[at:at + n]) == {"K": "high compact"}

        class SnapshotConsole:
            def command(self, command, timeout=30):
                _, _, read, address, size = command.split()
                assert read == "read"
                start = int(address)
                return "OK " + base64.b64encode(image[start:start + int(size)]).decode()

        payload_size, payload_crc = struct.unpack_from("<II", image, SEGMENT + 16)
        bitmap = "00000000" * 32
        live, _ = read_snapshot(SnapshotConsole(), f"OK {SEGMENT + 32} {payload_size} {payload_crc} {bitmap}",
                                trim_journals=True)
        assert live.pool_end == LARGE_POOL_END and live.extents == current.extents
        bundle = capture(lambda at, n: bytes(image[at:at + n]), current, "Mac U.S.")
        rebuilt, restored = reconstruct(bundle)
        assert bundle["pool_segments"] == restored.pool_end == LARGE_POOL_END
        assert effective_entries(restored, "High image", lambda at, n: rebuilt[at:at + n]) == {"K": "high compact"}
        journal = plan(current, set(range(768)), {(8,): b"high journal"})
        assert min(journal.segments) == 768

        class MapConsole:
            def __init__(self, stale=False):
                self.stale = stale

            def command(self, command, timeout=30):
                parts = command.split()
                first = int(parts[-1]) if len(parts) == 4 else 0
                layout = "cc" + "." * 510 + "s" + "." * 255 + "h" + "." * 254 + "x"
                page = layout[first:first + 256]
                generation = 6 if not (self.stale and first) else 7
                return f"OK MAP2 1024 {first} 5 {generation} 1 w vv {page} {page}"

        layout = read_layout(MapConsole())
        assert len(layout["segments"]) == 1024 and layout["segments"][768]["kind"] == "h"
        try:
            read_layout(MapConsole(stale=True))
        except RuntimeError as error:
            assert "catalog changed" in str(error)
        else:
            raise AssertionError("Accepted flash map pages from different generations")

        image_file, named_file, journal_file, binary = [
            folder / name for name in ("initial.bin", "named.plan", "journal.plan", "geometry")
        ]
        image_file.write_bytes(original)
        write_plan(named_file, named)
        write_plan(journal_file, journal)
        subprocess.run([*compiler(), "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-I", str(ROOT / "tools/storage"), str(ROOT / "tools/tests/test_native_geometry.cc"),
                        "-o", str(binary)], check=True)
        subprocess.run([str(binary), str(image_file), str(named_file), str(journal_file)], check=True)
        print("64 MiB geometry: compact image above 32 MiB and journal above 48 MiB published and reopened.")


if __name__ == "__main__":
    main()
