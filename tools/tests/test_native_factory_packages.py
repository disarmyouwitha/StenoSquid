#!/usr/bin/env python3
"""Exercise factory package planning, provenance, backup and NOR publication."""
import json
from pathlib import Path
import struct
import subprocess
import tempfile
from unittest.mock import Mock, patch
import zlib

from hostlib import compiler
from test_native_stage import ROOT, fixture, write_plan
from test_native_personal_snapshot import opened, publish_image
from build_native_image import SEGMENT, SOURCES
from compile_dict import DictionaryCollectionBuilder
from native_base_update import GROUPS, Image, factory_package, plan_package
from native_recovery import capture, reconstruct, recover_usb
from native_repack import build_repack
from native_update import NoSpace, effective_entries, plan, row_name


def refused(call, *args, **kwargs):
    try:
        call(*args, **kwargs)
    except (RuntimeError, ValueError, NoSpace) as error:
        return str(error)
    raise AssertionError("Accepted an invalid factory operation")


def ownership(view):
    return {0, 1, 255} | {i for start, count, *_ in view.extents for i in range(start, start + count)}


def main():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        image, view, protected = fixture(path)
        initial = path / "initial.bin"
        initial.write_bytes(image)
        binary = path / "factory"
        subprocess.run([*compiler(), "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-I", str(ROOT / "tools/storage"), str(ROOT / "tools/tests/test_native_factory_packages.cc"),
                        "-o", str(binary)], check=True)
        dormant, absent = image, view
        for key, names, identity in zip(("lapwing", "plover"), GROUPS, (6, 7), strict=True):
            definitions = [(name, json.loads((path / source).read_text())) for _, name, _, source in SOURCES
                           if name in names]
            builder = DictionaryCollectionBuilder(0)
            body = builder.build(definitions)
            compiled = Image(definitions, body, builder.pointer_offsets,
                             {row_name(row): row[64:96] for row in view.rows if row_name(row) in names})
            old_protected = ownership(absent)
            uninstall, preview = plan_package(absent, old_protected, key)
            assert preview["staging_segments"] == 1 and preview["package_segments_released"] == 1
            assert preview["free_after_one_root"] == preview["free_now"]
            assert preview["free_after_both_roots"] == preview["free_now"] + 1
            assert "reserve" in refused(plan_package, absent, set(range(252)) | {255}, key)
            before_file, remove_file = path / f"{key}-before.bin", path / f"{key}-remove"
            before_file.write_bytes(dormant)
            write_plan(remove_file, uninstall)
            dormant = publish_image(dormant, uninstall, 3)
            absent = opened(dormant)
            for name in names:
                row = next(row for row in absent.rows if row_name(row) == name)
                assert row[4:12] == b"\0\0\0\0\xff\xff\0\2" and row[64:96] == compiled.digests[name]
                refused(plan, absent, ownership(absent), delete=name)
            assert absent.next_id == view.next_id and absent.entries == view.entries
            restore, costs = plan_package(absent, ownership(absent), key, True, compiled)
            assert costs["staging_segments"] == 2 and costs["free_after_both_roots"] == costs["free_now"] - 1
            restored = publish_image(dormant, restore, 5)
            restored_view = opened(restored)
            for name in names:
                wanted = effective_entries(view, name, lambda at, n: image[at:at+n])
                assert effective_entries(restored_view, name, lambda at, n: restored[at:at+n]) == wanted
                row = next(row for row in restored_view.rows if row_name(row) == name)
                assert not row[4] and not row[11]
            audit_file, restore_file = path / f"{key}-absent.bin", path / f"{key}-restore"
            audit_file.write_bytes(dormant)
            write_plan(restore_file, restore)
            subprocess.run([str(binary), str(before_file), str(remove_file), str(audit_file),
                            str(restore_file), str(identity)], check=True)

        bundle = capture(lambda at, n: dormant[at:at+n], absent, "Mac U.S.")
        assert reconstruct(bundle)[1].entries == view.entries
        _, repacked_bundle, report = build_repack(bundle)
        assert not set(report["converted"]) & set(GROUPS[0] + GROUPS[1])
        assert report["verification"]["retained_overlay_records"] == len(view.entries)
        assert sum(row[11] == 2 for row in reconstruct(repacked_bundle)[1].rows) == 6

        console = Mock(serial="factory-test")
        console.command.side_effect = ["OK RECOVERY1", "ERR Unknown command"]
        from contextlib import nullcontext

        with patch("native_install.open_console", return_value=nullcontext(console)):
            assert "Update the target firmware" in refused(recover_usb, bundle, console.serial, console.serial)
        assert console.command.call_count == 2

        console.command.side_effect = lambda text, **_: "OK FACTORY1" if text.endswith("-capabilities") else "OK"
        with patch("native_base_update.read_snapshot", return_value=(view, protected)), patch(
                "native_base_update.publish") as publish:
            preview = factory_package(console, "plover")
            refused(factory_package, console, "plover", confirmation="wrong")
            publish.assert_not_called()
            factory_package(console, "plover", confirmation=preview["confirmation"])
            publish.assert_called_once()
        assert struct.unpack_from("<H", image, SEGMENT + 64 + 7 * 96)[0] == 2
        print("Factory packages: exact pinned restore, tombstones, reserve, stale approval, backup and repack passed")


if __name__ == "__main__":
    main()
