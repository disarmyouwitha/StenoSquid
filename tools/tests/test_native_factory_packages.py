#!/usr/bin/env python3
"""Exercise factory dictionary planning, provenance, backup and NOR publication."""
import json
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import Mock, patch
import zlib

from hostlib import compiler
from test_native_stage import ROOT, fixture, write_plan
from test_native_personal_snapshot import opened, publish_image
from build_native_image import build, FACTORY_KEYS, SOURCES
from compile_dict import DictionaryCollectionBuilder
from native_base_update import Image, factory_package, plan_package
from native_recovery import capture, reconstruct, recover_usb
from native_install import check_image, provision, UNPROVISIONED
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
        split, original = image, view
        installed_path = path / "split.bin"
        installed_path.write_bytes(split)
        for key, (identity, name, _, source) in FACTORY_KEYS.items():
            definition = [(name, json.loads((path / source).read_text()))]
            builder = DictionaryCollectionBuilder(0)
            compiled = Image(definition, builder.build(definition), builder.pointer_offsets,
                             {name: original.rows[identity - 1][64:96]})
            uninstall, _ = plan_package(original, ownership(original), key)
            reduced = publish_image(split, uninstall, 3)
            reduced_view = opened(reduced)
            assert [i + 1 for i, row in enumerate(reduced_view.rows) if row[11] == 2] == [identity]
            assert reduced_view.entries == original.entries
            restored, _ = plan_package(reduced_view, ownership(reduced_view), key, True, compiled)
            result = publish_image(reduced, restored, 5)
            assert effective_entries(opened(result), name, lambda at, n: result[at:at+n]) == effective_entries(
                original, name, lambda at, n: split[at:at+n])
            if key in ("lapwing-commands", "lapwing-base"):
                bundle = capture(lambda at, n: reduced[at:at+n], reduced_view, "Mac U.S.")
                assert reconstruct(bundle)[1].entries == original.entries
                _, packed, _ = build_repack(bundle)
                assert reconstruct(packed)[1].rows[identity - 1][11] == 2
                reduced_path, removal_path, restore_path = (path / f"split-{key}-{suffix}" for suffix in
                                                             ("reduced", "remove", "restore"))
                reduced_path.write_bytes(reduced)
                write_plan(removal_path, uninstall)
                write_plan(restore_path, restored)
                subprocess.run([str(binary), str(installed_path), str(removal_path), str(reduced_path),
                                str(restore_path), str(identity)], check=True)
        partial, manifest = build(path, packages=["lapwing-uk", "plover"])
        assert [item["name"] for item in manifest["base_images"]] == ["Lapwing UK Additions", "Plover Base"]
        assert check_image(partial, manifest) == {2, 3, 5, 6}
        absent = opened(partial)
        bundle = capture(lambda at, n: partial[at:at+n], absent, "Mac U.S.")
        setup_console = Mock()
        setup_console.command.side_effect = ["ERR Unsupported"]
        assert "Update the left firmware" in refused(provision, setup_console, partial, manifest)
        setup_console.reset_mock()
        setup_console.command.side_effect = ["OK FACTORY2", "NATIVE2 READY 2"]
        assert "status changed" in refused(provision, setup_console, partial, manifest,
                                           expected_status=UNPROVISIONED)
        assert setup_console.command.call_count == 2

        console = Mock(serial="factory-test")
        console.command.side_effect = ["ERR No native installation active", "OK RECOVERY1", "ERR Unknown command"]
        from contextlib import nullcontext

        with patch("native_install.open_console", return_value=nullcontext(console)):
            assert "Update the target firmware" in refused(recover_usb, bundle, console.serial, console.serial)
        assert console.command.call_count == 3

        console.command.side_effect = lambda text, **_: "OK FACTORY2" if text.endswith("-capabilities") else "OK"
        with patch("native_base_update.read_snapshot", return_value=(view, protected)), patch(
                "native_base_update.publish") as publish:
            preview = factory_package(console, "plover")
            refused(factory_package, console, "plover", confirmation="wrong")
            publish.assert_not_called()
            factory_package(console, "plover", confirmation=preview["confirmation"])
            publish.assert_called_once()
        print("Factory dictionaries: exact pinned restore, tombstones, reserve, stale approval, backup and repack passed")


if __name__ == "__main__":
    main()
