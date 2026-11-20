#!/usr/bin/env python3
"""Exercise the host-only Gate B session on a simulated Native spare."""
import base64
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import shutil
import struct
import sys
import tempfile
from unittest.mock import patch
import zlib

from test_native_recovery import Console
from test_native_personal_snapshot import opened, publish_image
from test_native_stage import ROOT, fixture
from build_native_image import SEGMENT, SOURCES, build
from compile_dict import parse_outline
from native_snapshot import change_snapshot, compile_snapshot
from import_personal import parse_named
from native_update import Edit, effective_entries, encode_record, row_name

sys.path.insert(0, str(ROOT / "tools/bench"))
import gate_b


class Spare(Console):
    serial = "fake-spare"
    generation = 2
    pending = None

    def __init__(self, image):
        super().__init__(bytearray(image), opened(image).payload)
        self.commands = []
        self.defer_map = True
        self.fail_remove = False

    def append(self, identity, outline, text, operation=0):
        view = opened(self.image)
        segment = view.anchor if view.anchor is not None else view.journals[-1][0]
        at = segment * SEGMENT + 256 + view.used[segment]
        record = encode_record(Edit(at, identity, operation, parse_outline(outline), text), view.highwater + 1)
        self.image[at:at + len(record)] = record

    def publish(self, update, progress):
        assert self.active
        self.image = publish_image(self.image, update, self.generation + 1)
        self.generation += 2
        self.payload = update.payload
        self.active = False
        progress("Simulated publication")

    def command(self, command, timeout=30):
        self.commands.append(command)
        view = opened(self.image)
        if command == "hello":
            return "OK test"
        if command == "native_install status":
            return f"NATIVE2 READY {self.generation}"
        if command == "user_dictionary_status":
            return json.dumps(dict(format="SQBCAT02", ready=True, read_only=False, generation=self.generation,
                                   ids=[struct.unpack_from("<I", row)[0] for row in view.rows]))
        if command == "native_catalog":
            return json.dumps([
                dict(id=struct.unpack_from("<I", row)[0], name=row_name(row), enabled=bool(row[4]),
                     kind="snapshot" if row[11] else "personal" if row_name(row) == gate_b.PERSONAL else "base")
                for row in view.rows
            ])
        if command.startswith("lookup_stroke "):
            outline = command.split()[1]
            for row in view.rows:
                if row[4]:
                    found = effective_entries(view, row_name(row), lambda at, n: self.image[at:at + n])
                    if outline in found:
                        return found[outline]
            return "ERR No translation"
        if command == "native_install update layout-map":
            if self.defer_map:
                return "ERR Layout scan deferred until the flash and input are idle"
            maps = "cc" + "." * 253 + "x"
            return f"OK MAP1 1 w ev {maps} {maps}"
        if command.removeprefix("native_install update ") in gate_b.CAPABILITIES:
            return gate_b.CAPABILITIES[command.split()[-1]]
        if command.startswith("native_install update begin snapshot-"):
            assert not self.active
            self.active = True
            protected = {0, 1, 255} | {i for start, count, *_ in view.extents for i in range(start, start + count)}
            words = "".join(f"{sum(1 << (i % 32) for i in protected if i // 32 == part):08x}" for part in range(8))
            return f"OK {SEGMENT + 32} {len(self.payload)} {zlib.crc32(self.payload)} {words}"
        if command == "native_entry abort":
            self.pending = None
            return "OK"
        if command.startswith("native_entry begin "):
            _, _, identity, _, outline = command.split()
            self.pending = [int(identity), outline, bytearray()]
            return "OK"
        if command.startswith("native_entry data "):
            self.pending[2].extend(base64.b64decode(command.split()[-1]))
            return "OK"
        if command == "native_entry commit":
            self.append(*self.pending)
            self.pending = None
            return "OK"
        if command.startswith("native_remove_entry "):
            if self.fail_remove:
                raise TimeoutError("simulated lost reply")
            _, identity, outline = command.split()
            self.append(int(identity), outline, b"", 1)
            return "OK"
        return super().command(command, timeout)


def main():
    with tempfile.TemporaryDirectory() as folder:
        directory = Path(folder)
        fixture(directory)
        for _, name, _, source in SOURCES:
            if source:
                content = {"KAT": "cat", "A/ROUPBD": "around", "PWOB": "Bob"} if name == "Lapwing Base" else {}
                if not content:
                    content = {"S": "is"}
                (directory / source).write_text(json.dumps(content))
        spare = Spare(build(directory)[0])
        uf2 = directory / "test.uf2"
        uf2.write_bytes(b"mock checkpoint")
        manifest = directory / "manifest.json"
        manifest.write_text(json.dumps(dict(commit="test", artifacts={"Fake": dict(url="offline",
            sha256=hashlib.sha256(uf2.read_bytes()).hexdigest(), bytes=uf2.stat().st_size)})))
        out = directory / "build" / "session"
        with patch("gate_b.CHECKPOINT", manifest), patch("gate_b.open_console", return_value=nullcontext(spare)) as connect, \
                patch("native_snapshot.publish", side_effect=lambda console, update, progress: console.publish(update, progress)), \
                patch("gate_b.wait_ready") as reboot:
            def stage(name, root=out):
                return gate_b.run(name, spare.serial, root, confirmation=spare.serial)

            try:
                gate_b.run("convert", spare.serial, out, confirmation="wrong")
            except ValueError:
                pass
            else:
                raise AssertionError("Unconfirmed write allowed")
            connect.assert_not_called()
            wrong = gate_b.run("baseline", "different-serial", directory / "wrong", uf2)
            assert wrong["result"] == "fail" and not spare.commands
            assert gate_b.run("baseline", spare.serial, out, uf2)["result"] == "pass"
            assert all(not line.startswith(("add_", "native_entry", "native_remove",
                                            "native_install erase", "native_install begin",
                                            "native_install update write", "native_install update erase"))
                       for line in spare.commands)
            baseline = gate_b.load(out / "baseline.json")
            assert baseline["layout"]["result"] == "deferred" and baseline["backup"]["offline_verified"]
            assert baseline["personal"]["count"] == 0 and len(baseline["exports"]) >= 3
            assert (out / "before-bundle.json").exists()
            backup = out / "before-bundle.json"
            data = backup.read_bytes()
            backup.write_bytes(data + b"tampered")
            before = len(spare.commands)
            assert stage("convert")["result"] == "fail" and len(spare.commands) == before
            backup.write_bytes(data)
            # A failed stage is blocked in its evidence folder even after the local issue is fixed.
            refused = stage("convert")
            assert refused["result"] == "fail" and not refused["write_may_have_run"]
            clean = directory / "verified"
            shutil.copytree(out, clean, ignore=shutil.ignore_patterns("*-failed-*.json"))
            spare.defer_map = False
            fill, _ = parse_named((gate_b.FIXTURES / "fill-personal.json").read_bytes())
            catalog_preview = change_snapshot(spare, "personal", gate_b.PERSONAL, source=fill, preview=True)
            assert catalog_preview["id"] == opened(spare.image).personal and not spare.active
            assert stage("convert", clean)["result"] == "pass"
            failed = directory / "interrupted"
            shutil.copytree(clean, failed)
            interrupted = Spare(spare.image)
            interrupted.generation = spare.generation
            interrupted.fail_remove = True
            with patch("gate_b.open_console", return_value=nullcontext(interrupted)):
                failed_edit = stage("edit", failed)
                assert failed_edit["result"] == "fail" and failed_edit["write_may_have_run"]
                assert gate_b.run("inspect", spare.serial, failed)["result"] == "pass"
                before = len(interrupted.commands)
                assert stage("edit", failed)["result"] == "fail" and len(interrupted.commands) == before
            assert stage("edit", clean)["result"] == "pass"
            consolidation = change_snapshot(spare, "consolidate", gate_b.PERSONAL, preview=True)
            assert consolidation["compact_bytes"] > 0 and not spare.active
            assert stage("consolidate", clean)["result"] == "pass"
            assert reboot.call_count == 2
            cold = gate_b.run("cold", spare.serial, clean)
            assert cold["result"] == "pass" and cold["personal_overlay_count"] == 0
            assert cold["personal"]["sha256"] == gate_b.load(clean / "edit.json")["personal"]["sha256"]
            assert gate_b.run("inspect", spare.serial, clean)["result"] == "pass"
            baseline = compile_snapshot(gate_b.PERSONAL, {parse_outline("KAT"): b"other cat"})
            plan = change_snapshot(spare, "replace", gate_b.PERSONAL, source=baseline, preview=True)
            assert plan["image_segments"] == 1 and plan["staging_segments"] == 2
            assert not spare.active
            variants = (
                (spare.generation, "different-spare", baseline),
                (spare.generation + 1, spare.serial, baseline),
                (spare.generation, spare.serial, compile_snapshot(gate_b.PERSONAL,
                    {parse_outline("KAT"): b"changed source"})),
            )
            original_generation, original_serial = spare.generation, spare.serial
            prior_image = spare.image[:]
            spare.append(opened(spare.image).personal, "KAT", b"another edit")
            try:
                change_snapshot(spare, "replace", gate_b.PERSONAL, source=baseline, stage=True, expected=plan["token"])
            except RuntimeError as exc:
                assert "plan changed" in str(exc)
            else:
                raise AssertionError("Uncheckpointed edit left approval valid")
            assert not spare.active
            spare.image = prior_image
            for generation, serial, compiled in variants:
                spare.generation, spare.serial = generation, serial
                try:
                    change_snapshot(spare, "replace", gate_b.PERSONAL, source=compiled, stage=True,
                                    expected=plan["token"])
                except RuntimeError as exc:
                    assert "plan changed" in str(exc)
                else:
                    raise AssertionError("Stale or cross-device approval was accepted")
                assert not spare.active and spare.generation == generation
            spare.generation, spare.serial = original_generation, original_serial
            assert change_snapshot(spare, "replace", gate_b.PERSONAL, source=baseline, stage=True,
                                   expected=plan["token"])
            assert spare.command("lookup_stroke KAT") == "other cat"
            assert not any("begin RECOVERY" in line or "begin CLEAR" in line for line in spare.commands)
    print("Gate B: read-only baseline, V2 backup, digest/serial gates, fixture lifecycle and cold readback passed")


if __name__ == "__main__":
    main()
