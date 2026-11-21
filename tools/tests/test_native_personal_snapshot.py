#!/usr/bin/env python3
"""Personal conversion plans, priority and effective translations after a cold open."""
from pathlib import Path
import struct
import subprocess
import tempfile
import zlib
from unittest.mock import Mock, patch

from hostlib import compiler
from test_native_stage import ROOT, fixture, write_plan

from build_native_image import SEGMENT, SOURCES, build, catalog_header
from compile_dict import parse_outline
from native_snapshot import change_snapshot, compile_snapshot, journal_baseline, personal_baseline, plan_change
from native_update import Edit, NoSpace, Snapshot, effective_entries, encode_record, plan, row_name


def opened(image):
    at = SEGMENT
    length, crc = struct.unpack_from("<II", image, at + 16)
    return Snapshot(lambda address, n: image[address:address + n], at + 32, length, crc)


def publish_image(image, update, generation):
    result = bytearray(image)
    for segment, block in update.segments.items():
        at = segment * SEGMENT
        result[at:at + len(block)] = block
    for slot in range(2):
        at = slot * SEGMENT
        payload = catalog_header(generation + slot, update.payload) + update.payload
        result[at:at + len(payload)] = payload
    return result


def main():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        image, snapshot, protected = fixture(path)
        empty_image, _ = build(path)
        console = Mock()
        console.command.side_effect = [
            '[{"id":1,"name":"user_dictionary","kind":"personal"}]', '{"generation":2}'
        ]
        with patch("native_snapshot.update_command", side_effect=["OK SNAPSHOT2", "OK PERSONAL1", "OK"]), \
                patch("native_snapshot.read_snapshot", return_value=(opened(empty_image), protected)), \
                patch("native_snapshot.abort"), patch("native_snapshot.publish") as publish:
            preview = change_snapshot(console, "personal", "user_dictionary", preview=True)
            assert preview["token"] is None and preview["reason"] == "This journal has no entries to compact."
            publish.assert_not_called()
        moved = plan(snapshot, protected, move=("user_dictionary", 4))
        image = publish_image(image, moved, 3)
        payload = bytearray(moved.payload)
        struct.pack_into("<I", payload, 32 + 3 * 96 + 4, 0)  # disabled personal row
        for slot in range(2):
            at = slot * SEGMENT
            image[at:at + 32 + len(payload)] = catalog_header(3 + slot, payload) + payload
        before = opened(image)
        kat, bob, tas = (parse_outline(outline) for outline in ("KAT", "PWOB", "TAS"))
        edits = ((0, kat, b"local cat"), (1, bob, b""))
        offset = before.anchor * SEGMENT + 256 + before.used[before.anchor]
        for sequence, (op, outline, text) in enumerate(edits, before.highwater + 1):
            entry = Edit(offset, before.personal, op, outline, text)
            record = encode_record(entry, sequence)
            image[offset:offset + len(record)] = record
            offset += entry.span
        before = opened(image)
        baseline = personal_baseline(before, {kat: b"imported cat", bob: b"Bob", tas: b"addition"})
        assert baseline[kat] == b"local cat" and bob not in baseline and baseline[tas] == b"addition"
        compiled = compile_snapshot("user_dictionary", baseline)
        update = plan_change(before, protected, before.personal, compiled, convert=True)
        recovered = publish_image(image, update, 5)
        current = opened(recovered)
        assert current.personal == before.personal and current.next_id == before.next_id
        assert row_name(current.rows[3]) == "user_dictionary" and current.rows[3][:8] == before.rows[3][:8]
        assert current.rows[3][11] == 1 and current.entries[(before.personal, bob)].operation == 1
        assert (before.personal, kat) not in current.entries
        assert effective_entries(current, "user_dictionary", lambda at, n: recovered[at:at + n])["KAT"] == "local cat"
        assert {entry.key for entry in current.entries.values() if entry.identity != before.personal} == {
            entry.key for entry in before.entries.values() if entry.identity != before.personal}
        try:
            plan_change(before, set(range(252)) | {255}, before.personal, compiled, convert=True)
        except NoSpace:
            pass
        else:
            raise AssertionError("Personal conversion consumed the maintenance reserve")
        before_file, plan_file, binary = (path / name for name in ("old.bin", "convert.bin", "test"))
        before_file.write_bytes(image)
        write_plan(plan_file, update)
        converted_file, clear_file = (path / name for name in ("converted.bin", "clear.bin"))
        converted_file.write_bytes(recovered)
        clear = plan_change(current, protected | set(update.segments), current.personal, clear=True)
        write_plan(clear_file, clear)
        cleared_image = publish_image(recovered, clear, 7)
        cleared = opened(cleared_image)
        assert cleared.rows[3][:8] == before.rows[3][:8] and cleared.rows[3][8:12] == b"\xff\xff\0\0"
        assert not effective_entries(cleared, "user_dictionary", lambda at, n: cleared_image[at:at + n])
        assert {entry.key for entry in cleared.entries.values()} == {
            entry.key for entry in current.entries.values() if entry.identity != current.personal}
        subprocess.run([*compiler(), "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-I", str(ROOT / "tools/storage"),
                        str(ROOT / "tools/tests/test_native_personal_snapshot.cc"), "-o", str(binary)], check=True)
        subprocess.run([str(binary), str(before_file), str(plan_file)], check=True)
        subprocess.run([str(binary), str(converted_file), str(clear_file), "clear"], check=True)
        write_plan(clear_file, plan_change(before, protected, before.personal, clear=True))
        subprocess.run([str(binary), str(before_file), str(clear_file), "clear"], check=True)

        rows = [(identity, name, enabled) for identity, name, enabled, _ in SOURCES]
        rows.insert(3, (8, "Named journal", False))
        records = {(8, kat): (0, b"named cat"), (8, bob): (1, b""), (2, (6,)): (0, b"pinned edit")}
        named_image, _ = build(path, collection=(rows, records, 9))
        named = opened(named_image)
        protected = {0, 1, 255} | {segment for start, count, *_ in named.extents
                                  for segment in range(start, start + count)}
        compiled = compile_snapshot("Named journal", journal_baseline(named, 8))
        update = plan_change(named, protected, 8, compiled, convert=True)
        result = publish_image(named_image, update, 3)
        converted_named = opened(result)
        assert converted_named.rows[3][:8] == named.rows[3][:8] and converted_named.rows[3][11] == 1
        assert converted_named.next_id == named.next_id
        assert effective_entries(converted_named, "Named journal", lambda at, n: result[at:at + n]) == {"KAT": "named cat"}
        before_file.write_bytes(named_image)
        write_plan(plan_file, update)
        subprocess.run([str(binary), str(before_file), str(plan_file), "named"], check=True)


if __name__ == "__main__":
    main()
