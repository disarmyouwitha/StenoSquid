#!/usr/bin/env python3
"""Offline repack parity, relocation, refusals and recovered target in strict NOR."""
from pathlib import Path
import json
import struct
import subprocess
import tempfile
import zlib

from hostlib import compiler
from test_native_stage import ROOT, fixture
from test_native_recovery import Console
from test_native_personal_snapshot import opened, publish_image
from build_native_image import SEGMENT, SOURCES, build
from compile_dict import parse_outline
from native_install import check_image
from native_recovery import digest
from native_repack import repack
from native_snapshot import compile_snapshot, plan_named_snapshot
from native_update import backup_all, row_name


def refuse(function, *values):
    try:
        function(*values)
    except ValueError:
        return
    raise AssertionError("Unsafe or corrupt operation was accepted")


def main():
    with tempfile.TemporaryDirectory() as folder:
        directory = Path(folder)
        fixture(directory)  # Materialize the miniature pinned JSON sources.
        kat, bob, multi, other = (parse_outline(word) for word in ("KAT", "PWOB", "KAT/TA", "TA/KAT"))
        rows = [(row[0], row[1], row[2]) for row in SOURCES if row[3]]
        rows.insert(1, (20, "user_dictionary", False))
        rows.extend([(21, "Unrepresentable", True), (22, "Tiny", True)])
        operations = {
            (20, (1025 + i // 64, 200 + i % 64)): (0, f"repeated {i % 47}".encode())
            for i in range(7500)
        }
        operations.update({(20, kat): (0, b"personal cat"), (20, multi): (0, b"personal catalog"),
                           (20, other): (0, b"personal trap"), (20, bob): (1, b""),
                           (2, kat): (0, b"pinned override"), (3, bob): (1, b""),
                           (21, (1 << 24,)): (0, b"legal only in the Native journal"),
                           (22, kat): (0, b"late and lower priority")})
        data, _ = build(directory, collection=(rows, operations, 23))
        old = opened(data)
        layout = lambda view: {0, 1, 255} | {i for start, count, *_ in view.extents
                                                   for i in range(start, start + count)}
        legacy = backup_all(Console(data, old.payload))
        complete = backup_all(Console(data, old.payload), complete=True)
        assert legacy["format"] == "stenosquid-backup-1"
        assert complete["format"] == "stenosquid-backup-2"
        # A packed snapshot appears after the large source journal/indexes, so
        # rebuilding must move it and rebase its absolute fields.
        existing = compile_snapshot("Keep Compact", {(8,): b"stay compact", (8, 16): b"stay multistroke"})
        update = plan_named_snapshot(old, layout(old), "Keep Compact", existing)
        data = publish_image(data, update, 3)
        old = opened(data)
        complete = backup_all(Console(data, old.payload), complete=True)
        complete["host_layout"] = "Windows U.S. (plain text)"
        complete["sha256"] = digest(complete)

        target, report = repack(complete)
        again, second = repack(complete)
        assert (again, second) == (target, report)
        assert report["converted"] == ["user_dictionary"]
        assert "24-bit" in report["refused"]["Unrepresentable"]
        assert "savings" in report["refused"]["Tiny"]
        assert report["saved_segments"] >= 2 and report["target_layout"]["free_after_reserve"] > 0
        assert report["target_layout"]["largest_free_run"] == report["target_layout"]["free_segments"]
        assert report["host_layout"] == complete["host_layout"]
        after = opened(target)
        assert after.personal == 20 and after.next_id == 24
        assert after.rows[1][4] == 0 and after.rows[1][11] == 1
        assert after.entries[20, bob].operation == 1
        assert after.entries[2, kat].text == b"pinned override"
        assert after.entries[3, bob].operation == 1

        def compact_position(view, name):
            row = next(row for row in view.rows if row_name(row) == name)
            base = struct.unpack_from("<H", row, 8)[0]
            return view.extents[base][0]

        assert compact_position(after, "Keep Compact") < compact_position(old, "Keep Compact")
        _, keep = repack(complete, [])
        assert keep["saved_segments"] <= report["saved_segments"] and not keep["converted"]
        _, refusal = repack(complete, ["Plover Base", "Missing"])
        assert set(refusal["refused"]) == {"Plover Base", "Missing"}
        refuse(repack, dict(complete, sha256="bad"))
        refuse(check_image, target, report)
        refuse(check_image, target, dict(format="SQBCAT02", crc32=zlib.crc32(target),
                                         sha256=report["target_image_sha256"]))

        raw, bundle, summary, binary = (directory / name for name in ("repacked.bin", "input.json", "report.json", "nor"))
        bundle.write_text(json.dumps(complete), encoding="utf-8")
        command = ["python3", str(ROOT / "tools/storage/native_repack.py"), str(bundle),
                   "--report", str(summary), "--image", str(raw)]
        subprocess.run(command, check=True)
        assert raw.read_bytes() == target
        assert json.loads(summary.read_text()) == report
        process = subprocess.run(command, capture_output=True, text=True)
        assert process.returncode and "new files" in process.stderr
        assert raw.read_bytes() == target and json.loads(summary.read_text()) == report
        original = directory / "source.bin"
        original.write_bytes(data)
        subprocess.run([*compiler(), "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-DSTENO_SQUID_TEST_USER_SNAPSHOTS", "-I", str(ROOT / "tools/storage"),
                        "-I", str(ROOT / "src/steno_squid"), str(ROOT / "tools/tests/test_native_repack.cc"),
                        "-o", str(binary)], check=True)
        subprocess.run([str(binary), str(original), str(raw)], check=True)
        print(f"Packed fixture saved {report['saved_segments']} segments with {report['verification']['per_dictionary_entries']:,}"
              " exact per-dictionary translations")


if __name__ == "__main__":
    main()
