#!/usr/bin/env python3
"""Offline repack parity, relocation, refusals and recovered target in strict NOR."""
import hashlib
from pathlib import Path
import json
import struct
import subprocess
import tempfile
from unittest.mock import patch
import zlib

from hostlib import compiler
from test_native_stage import ROOT, fixture
from test_native_recovery import Console
from test_native_personal_snapshot import opened, publish_image
from build_native_image import SEGMENT, SOURCES, build
from compile_dict import parse_outline
from native_install import check_image
from native_recovery import digest, reconstruct, save_atomic
from native_repack import check_live, pack_layout, repack, restore_spare, validate_pair
from native_snapshot import compile_snapshot, plan_named_snapshot
from native_update import backup_all, row_name


def refuse(function, *values):
    try:
        function(*values)
    except (ValueError, FileExistsError):
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
        complete = backup_all(Console(data, old.payload))
        assert complete["format"] == "stenosquid-backup-2"
        # A packed snapshot appears after the large source journal/indexes, so
        # rebuilding must move it and rebase its absolute fields.
        existing = compile_snapshot("Keep Compact", {(8,): b"stay compact", (8, 16): b"stay multistroke"})
        update = plan_named_snapshot(old, layout(old) | set(range(3, 110)), "Keep Compact", existing)
        data = publish_image(data, update, 3)
        old = opened(data)
        more = compile_snapshot("Second Compact", {(16,): b"other hint", (16, 8): b"second multistroke"})
        data = publish_image(data, plan_named_snapshot(old, layout(old) | set(range(3, 200)), "Second Compact", more), 5)
        old = opened(data)
        complete = backup_all(Console(data, old.payload))
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
        assert after.personal == 20 and after.next_id == 25
        assert after.rows[1][4] == 0 and after.rows[1][11] == 1
        assert after.entries[20, bob].operation == 1
        assert after.entries[2, kat].text == b"pinned override"
        assert after.entries[3, bob].operation == 1

        def compact_position(view, name):
            row = next(row for row in view.rows if row_name(row) == name)
            base = struct.unpack_from("<H", row, 8)[0]
            return view.extents[base][0]

        for name in ("Keep Compact", "Second Compact"):
            assert compact_position(after, name) < compact_position(old, name)
        assert report["target_same_size_consolidation"]["user_dictionary"]["fits"]
        refuse(pack_layout, None, None, None, None, 251)
        _, keep = repack(complete, [])
        assert keep["saved_segments"] == 0 and not keep["converted"]
        assert keep["recommendation"] == "contiguous-space-only"
        assert keep["source_layout"]["largest_free_run"] < keep["source_layout"]["free_segments"]
        assert keep["target_layout"]["largest_free_run"] > keep["source_layout"]["largest_free_run"]
        _, refusal = repack(complete, ["Plover Base", "Missing"])
        assert set(refusal["refused"]) == {"Plover Base", "Missing"}
        refuse(repack, dict(complete, sha256="bad"))
        refuse(check_image, target, report)
        refuse(check_image, target, dict(format="SQBCAT02", crc32=zlib.crc32(target),
                                         sha256=report["target_image_sha256"]))

        raw, bundle, summary, binary, optimized = (directory / name for name in
            ("repacked.bin", "input.json", "report.json", "nor", "optimized.json"))
        bundle.write_text(json.dumps(complete), encoding="utf-8")
        command = ["python3", str(ROOT / "tools/storage/native_repack.py"), str(bundle),
                   "--report", str(summary), "--image", str(raw), "--bundle-out", str(optimized)]
        subprocess.run(command, check=True)
        assert raw.read_bytes() == target
        saved = json.loads(summary.read_text())
        optimized_bytes = optimized.read_bytes()
        assert saved["source_file_sha256"] == hashlib.sha256(bundle.read_bytes()).hexdigest()
        assert saved["target_file_sha256"] == hashlib.sha256(optimized_bytes).hexdigest()
        assert {key: saved[key] for key in report} == report
        optimized_bundle = json.loads(optimized_bytes)
        assert reconstruct(optimized_bundle)[0] == target
        _, no_op = repack(optimized_bundle)
        assert no_op["recommendation"] == "no-operation"
        checked = subprocess.run(command[:3] + ["--verify-target", str(optimized)], capture_output=True, text=True, check=True)
        assert json.loads(checked.stdout)["verification"] == report["verification"]
        false_target = dict(optimized_bundle, host_layout="Mac U.S.")
        false_target["sha256"] = digest(false_target)
        refuse(validate_pair, complete, false_target, reconstruct(complete), reconstruct(false_target))
        process = subprocess.run(command, capture_output=True, text=True)
        assert process.returncode and "new files" in process.stderr
        assert raw.read_bytes() == target and json.loads(summary.read_text()) == saved
        assert optimized.read_bytes() == optimized_bytes
        inplace = subprocess.run(command[:3] + ["--report", str(directory / "unused"),
                                 "--bundle-out", str(bundle)], capture_output=True, text=True)
        assert inplace.returncode and hashlib.sha256(bundle.read_bytes()).hexdigest() == saved["source_file_sha256"]

        # An append with the same catalog generation still invalidates live preflight.
        class ReadOnly:
            def command(self, line, timeout=30):
                raise AssertionError(f"Stale verification reached a device command: {line}")

        edited = dict(complete, extents=[*complete["extents"][:-1], "unexpected edited bytes"])
        with patch("native_update.backup_all", return_value=edited):
            try:
                check_live(ReadOnly(), complete)
            except RuntimeError as error:
                assert "journal" in str(error)
            else:
                raise AssertionError("Stale live pool accepted")
        with patch("native_update.backup_all", return_value=complete):
            assert check_live(ReadOnly(), complete)["referenced_bytes_match"]
        provenance = {"serial": "source-mac", "generation": 7, "timestamp": "2026-11-20T01:00:00Z"}
        with patch("native_update.backup_all", return_value={**complete, "capture": {**provenance, "generation": 8}}):
            try:
                check_live(ReadOnly(), {**complete, "capture": provenance})
            except RuntimeError as error:
                assert "generation" in str(error)
            else:
                raise AssertionError("Different generation passed live-source verification")

        confirmation = "spare:" + saved["target_file_sha256"]
        with patch("native_repack.recover_usb") as recover:
            refuse(restore_spare, bundle, optimized, "spare", "spare")
            refuse(restore_spare, bundle, bundle, "spare", confirmation)
            recover.assert_not_called()
            restore_spare(bundle, optimized, "spare", confirmation, lambda _: None)
            assert recover.call_args.kwargs == {"repack_spare": True}
        originals = bundle.read_bytes(), optimized.read_bytes()
        refuse(save_atomic, bundle, {"bad": True})
        assert (bundle.read_bytes(), optimized.read_bytes()) == originals
        original = directory / "source.bin"
        original.write_bytes(data)
        subprocess.run([*compiler(), "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-I", str(ROOT / "tools/storage"),
                        "-I", str(ROOT / "src/steno_squid"), str(ROOT / "tools/tests/test_native_repack.cc"),
                        "-o", str(binary)], check=True)
        subprocess.run([str(binary), str(original), str(raw)], check=True)
        print(f"Packed fixture saved {report['saved_segments']} segments with {report['verification']['per_dictionary_entries']:,}"
              " exact per-dictionary translations")


if __name__ == "__main__":
    main()
