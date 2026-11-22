"""JSON editing validates complete drafts and preserves unrelated dictionaries."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manager  # Establish the shared storage/compiler import paths.
import manager_editor as editor


class Console:
    serial = "editor-board"

    def __init__(self):
        self.commands = []
        self.read_only = False
        self.commits = 0
        self.fail = False

    def command(self, text, timeout=30):
        self.commands.append(text)
        if text == "user_dictionary_status":
            return json.dumps(dict(ready=True, read_only=self.read_only))
        if text.startswith("native_entry begin") and self.fail and self.commits:
            return "ERR Storage full"
        if text == "native_entry commit":
            self.commits += 1
        return "OK"


class EditorTest(unittest.TestCase):
    def setUp(self):
        self.console = Console()
        self.selected = dict(id=8, name="Medical", kind="named")
        self.entries = {"KAT": "cat", "TKOG": "dog"}
        mock = patch.object(editor, "export_dictionary", side_effect=lambda *_, **kwargs: dict(self.entries))
        mock.start()
        self.addCleanup(mock.stop)
        self.document = editor.read_document(self.console, self.selected)
        self.console.commands.clear()

    def request(self, source):
        return dict(source=source, version=self.document["version"])

    def test_add_edit_remove_and_empty_json(self):
        request = self.request('{"KAT":"kitty", "HAT":"hat"}')
        plan = editor.change_document(self.console, self.selected, request, lambda _: None)
        self.assertEqual((plan["added"], plan["updated"], plan["removed"]), (1, 1, 1))
        self.assertTrue(all(command == "user_dictionary_status" for command in self.console.commands))
        saved = editor.change_document(self.console, self.selected, dict(request, token=plan["token"]), lambda _: None, apply=True)
        self.assertEqual(saved["changes"], 3)
        self.assertEqual(json.loads(saved["source"]), {"KAT": "kitty", "HAT": "hat"})
        self.assertIn("native_remove_entry 8 TKOG", self.console.commands)
        self.assertTrue(all(" 1 " not in text for text in self.console.commands if text.startswith("native_entry begin")))
        empty = editor.change_document(self.console, self.selected, self.request('{}'), lambda _: None)
        self.assertEqual((empty["added"], empty["removed"], empty["count"]), (0, 2, 0))

    def test_stale_draft_and_changed_preview_never_write(self):
        request = self.request('{"KAT":"kitty"}')
        plan = editor.change_document(self.console, self.selected, request, lambda _: None)
        self.entries["HAT"] = "external edit"
        with self.assertRaisesRegex(ValueError, "changed since"):
            editor.change_document(self.console, self.selected, dict(request, token=plan["token"]), lambda _: None, apply=True)
        del self.entries["HAT"]
        with self.assertRaisesRegex(ValueError, "Preview"):
            editor.change_document(self.console, self.selected, dict(request, source='{}', token=plan["token"]), lambda _: None, apply=True)
        self.assertTrue(all(command == "user_dictionary_status" for command in self.console.commands))

    def test_invalid_drafts_never_write(self):
        for source in ['{"KAT":', '{"KAT":"a","KAT":"b"}', '["cat"]', '{"KAT":123}', '{"KAT":"\\u0000"}']:
            with self.subTest(source=source), self.assertRaises(ValueError):
                editor.change_document(self.console, self.selected, self.request(source), lambda _: None, apply=True)
        self.assertTrue(all(command == "user_dictionary_status" for command in self.console.commands))

    def test_read_only_and_dictionary_identity_protected(self):
        request = self.request('{}')
        plan = editor.change_document(self.console, self.selected, request, lambda _: None)
        self.console.read_only = True
        with self.assertRaisesRegex(ValueError, "read-only"):
            editor.change_document(self.console, self.selected, dict(request, token=plan["token"]), lambda _: None, apply=True)
        self.assertTrue(all(command == "user_dictionary_status" for command in self.console.commands))
        self.console.commands.clear()
        with self.assertRaisesRegex(ValueError, "changed since"):
            editor.change_document(self.console, dict(self.selected, id=9), request, lambda _: None)
        self.assertTrue(all(command == "user_dictionary_status" for command in self.console.commands))

    def test_factory_changes_are_only_overlay_writes(self):
        self.selected["kind"] = "base"
        self.document = editor.read_document(self.console, self.selected)
        request = self.request('{}')
        plan = editor.change_document(self.console, self.selected, request, lambda _: None)
        editor.change_document(self.console, self.selected, dict(request, token=plan["token"]), lambda _: None, apply=True)
        self.assertEqual(self.console.commands[0], "user_dictionary_status")
        self.assertCountEqual([command for command in self.console.commands if command != "user_dictionary_status"], ["native_remove_entry 8 KAT", "native_remove_entry 8 TKOG"])

    def test_partial_save_reports_progress_and_requires_reload(self):
        self.console.fail = True
        request = self.request('{"KAT":"kitty", "TKOG":"puppy"}')
        plan = editor.change_document(self.console, self.selected, request, lambda _: None)
        with self.assertRaisesRegex(RuntimeError, "Saved 1/2 changes.*reload"):
            editor.change_document(self.console, self.selected, dict(request, token=plan["token"]), lambda _: None, apply=True)

    def test_cache_invalidates_when_keyboard_changes(self):
        status = {"ready": True, "generation": 1, "tail": 0}
        self.console.command = lambda *_: json.dumps(status)
        editor._documents.clear()
        with patch.object(editor, "export_dictionary", return_value={"KAT": "cat"}) as export:
            first = editor.read_document(self.console, self.selected)
            self.assertEqual(editor.read_document(self.console, self.selected), first)
            self.assertEqual(export.call_count, 1)
            editor.read_document(self.console, self.selected, force=True)
            self.assertEqual(export.call_count, 2)
            status["tail"] = 1
            editor.read_document(self.console, self.selected)
            self.assertEqual(export.call_count, 3)
            editor.clear_documents(self.console.serial)
            editor.read_document(self.console, self.selected)
            self.assertEqual(export.call_count, 4)
        editor._documents.clear()

    def test_matching_factory_source_reads_only_overlay(self):
        from types import SimpleNamespace
        from compile_dict import parse_outline
        from native_update import Edit
        import struct
        selected = dict(id=5, name="Lapwing Proper Nouns", kind="base", source_sha256="aa" * 32)
        row = bytearray(96)
        label = selected["name"].encode()
        struct.pack_into("<IIH", row, 0, 5, 1, 0)
        row[12] = len(label)
        row[16:16 + len(label)] = label
        row[64:96] = bytes.fromhex(selected["source_sha256"])
        rows = [Edit(0, 5, 0, parse_outline("KAT"), b"kitty"),
                Edit(0, 5, 1, parse_outline("TKOG"), b""),
                Edit(0, 9, 0, parse_outline("HAT"), b"unrelated")]
        snapshot = SimpleNamespace(rows=[bytes(row)], entries={entry.key: entry for entry in rows})
        with patch.object(editor, "factory_source", return_value={"KAT": "cat", "TKOG": "dog"}), \
                patch.object(editor, "update_command") as command, \
                patch.object(editor, "read_snapshot", return_value=(snapshot, set())), \
                patch.object(editor, "export_dictionary") as export:
            result = editor.effective_document(self.console, selected, lambda _: None, lambda _: None)
            self.assertEqual(result, {"KAT": "kitty"})
            export.assert_not_called()
            self.assertEqual([call.args[1] for call in command.call_args_list], ["begin backup", "abort"])

    def test_unknown_factory_source_falls_back_to_device(self):
        with patch.object(editor, "factory_source", return_value=None), \
                patch.object(editor, "export_dictionary", return_value={"KAT": "actual keyboard"}) as export:
            result = editor.effective_document(self.console, dict(self.selected, kind="base"), lambda _: None, lambda _: None)
            self.assertEqual(result, {"KAT": "actual keyboard"})
            export.assert_called_once()

    def test_bundled_proper_nouns_matches_compiled_dictionary(self):
        from compile_dict import DictionaryCollectionBuilder
        from hybrid import compact_entries
        from build_native_image import FACTORY_KEYS
        import hashlib
        identity, name, _, source = FACTORY_KEYS["lapwing-proper"]
        raw = (editor.ROOT / "build" / source).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        baseline = editor.factory_source(identity, name, digest)
        self.assertIsNotNone(baseline)
        compiled = DictionaryCollectionBuilder(0).build([(name, json.loads(raw))])
        self.assertEqual(baseline, dict(compact_entries(compiled, 0)))
        self.assertIsNone(editor.factory_source(identity, name, "00" * 32))

    def edits_document(self, data):
        return patch.object(editor, "saved_edits", return_value=data)

    def test_removing_edits_resets_instead_of_hiding_entries(self):
        data = {"KAT": "kitty", "TKOG": None, "HAT": "added"}
        with self.edits_document(data):
            doc = editor.read_document(self.console, self.selected, mode="edits")
            request = dict(source='{"HAT":"added"}', version=doc["version"], mode="edits")
            plan = editor.change_document(self.console, self.selected, request, lambda _: None)
            self.assertEqual(plan["removed"], 2)
            self.console.commands.clear()
            editor.change_document(self.console, self.selected, dict(request, token=plan["token"]), lambda _: None, apply=True)
            writes = [command for command in self.console.commands if command.startswith("native_")]
            self.assertCountEqual(writes, ["native_reset_entry 8 KAT", "native_reset_entry 8 TKOG"])

    def test_empty_edits_clears_every_override_and_preserves_baseline(self):
        data = {"KAT": "kitty", "TKOG": None, "HAT": "added"}
        with self.edits_document(data):
            doc = editor.read_document(self.console, self.selected, mode="edits")
            request = dict(source='{}', version=doc["version"], mode="edits")
            plan = editor.change_document(self.console, self.selected, request, lambda _: None)
            self.assertEqual((plan["removed"], plan["count"]), (3, 0))
            result = editor.change_document(self.console, self.selected, dict(request, token=plan["token"]), lambda _: None, apply=True)
            self.assertEqual(json.loads(result["source"]), {})
            self.assertEqual(self.entries, {"KAT": "cat", "TKOG": "dog"})
            self.assertEqual(len([command for command in self.console.commands if command.startswith("native_reset_entry 8 ")]), 3)
            self.assertFalse(any(command.startswith("native_remove_entry") for command in self.console.commands))

    def test_edits_and_whole_dictionary_versions_are_not_interchangeable(self):
        with self.edits_document(self.entries):
            request = dict(source='{}', version=self.document["version"], mode="edits")
            with self.assertRaisesRegex(ValueError, "changed since"):
                editor.change_document(self.console, self.selected, request, lambda _: None)
        with self.assertRaises(ValueError):
            editor.parse_source('{"KAT":null}')
        with self.assertRaises(ValueError):
            editor.parse_edits('{"KAT":"x","KAT":null}')

    def test_snapshot_undo_requires_firmware_before_any_write(self):
        self.selected["kind"] = "snapshot"
        with self.edits_document({"KAT": "kitty"}):
            doc = editor.read_document(self.console, self.selected, mode="edits")
            request = dict(source='{"HAT":"added"}', version=doc["version"], mode="edits")
            plan = editor.change_document(self.console, self.selected, request, lambda _: None)
            self.console.commands.clear()
            with self.assertRaisesRegex(ValueError, "Update the left firmware"):
                editor.change_document(self.console, self.selected, dict(request, token=plan["token"]), lambda _: None, apply=True)
            self.assertFalse(any(command.startswith(("native_entry ", "native_reset_entry")) for command in self.console.commands))
            original = self.console.command
            self.console.command = lambda command, **kw: "OK RESET1" if command.endswith("snapshot-reset-capabilities") else original(command, **kw)
            result = editor.change_document(self.console, self.selected, dict(request, token=plan["token"]), lambda _: None, apply=True)
            self.assertEqual(result["changes"], 2)

    def cache_fixture(self, kind):
        from types import SimpleNamespace
        from compile_dict import parse_outline
        from native_update import Edit
        selected = dict(self.selected, kind=kind, source_sha256="aa" * 32)
        row = bytearray(96)
        label = selected["name"].encode()
        import struct
        struct.pack_into("<IIH", row, 0, selected["id"], 1, 0)
        row[11] = int(kind == "snapshot")
        row[12] = len(label)
        row[16:16 + len(label)] = label
        row[64:96] = bytes.fromhex(selected["source_sha256"])
        entries = [Edit(0, selected["id"], 0, parse_outline("KAT"), b"kitty"),
                   Edit(0, selected["id"], 1, parse_outline("TKOG"), b"")]
        return selected, SimpleNamespace(rows=[bytes(row)], entries={entry.key: entry for entry in entries})

    def test_whole_and_edits_share_one_snapshot_for_factory_and_added_compact(self):
        for kind in ("base", "snapshot"):
            with self.subTest(kind=kind):
                selected, snapshot = self.cache_fixture(kind)
                editor._documents.clear()
                self.console.command = lambda *_: json.dumps({"ready": True, "generation": 1, "tail": 2})

                def export(*args, **kwargs):
                    kwargs["on_snapshot"](snapshot)
                    return {"KAT": "kitty"}

                with patch.object(editor, "factory_source", return_value={"KAT": "cat", "TKOG": "dog"}), \
                        patch.object(editor, "read_snapshot", return_value=(snapshot, set())) as read, \
                        patch.object(editor, "update_command"), \
                        patch.object(editor, "export_dictionary", side_effect=export) as full, \
                        patch.object(editor, "saved_edits", side_effect=AssertionError("Unexpected second keyboard read")):
                    whole = editor.read_document(self.console, selected)
                    edits = editor.read_document(self.console, selected, mode="edits")
                    self.assertEqual(json.loads(edits["source"]), {"KAT": "kitty", "TKOG": None})
                    self.assertEqual(editor.read_document(self.console, selected), whole)
                    self.assertEqual(read.call_count + full.call_count, 1)
                editor._documents.clear()

    def test_edits_first_also_primes_matching_factory_whole_view(self):
        selected, snapshot = self.cache_fixture("base")
        self.console.command = lambda *_: json.dumps({"ready": True, "generation": 1, "tail": 2})
        editor._documents.clear()
        with patch.object(editor, "factory_source", return_value={"KAT": "cat", "TKOG": "dog"}), \
                patch.object(editor, "read_snapshot", return_value=(snapshot, set())) as read, \
                patch.object(editor, "update_command"), \
                patch.object(editor, "export_dictionary", side_effect=AssertionError("Unexpected base read")):
            editor.read_document(self.console, selected, mode="edits")
            full = editor.read_document(self.console, selected)
            self.assertEqual(json.loads(full["source"]), {"KAT": "kitty"})
            self.assertEqual(read.call_count, 1)
        editor._documents.clear()

    def journal_fixture(self, records):
        import base64, struct, zlib
        from build_native_image import SEGMENT, write_journal_header
        from native_update import Edit, encode_record
        image = bytearray(b"\xff" * (3 * SEGMENT))
        extent = write_journal_header(image, 2, 1)
        row = bytearray(96)
        struct.pack_into("<IIH", row, 0, 1, 1, 65535)
        label = b"user_dictionary"
        row[12] = len(label)
        row[16:16 + len(label)] = label
        payload = struct.pack("<IHHQ16x", 2, 1, 1, 0) + row + extent
        image[32:32 + len(payload)] = payload
        offset = 2 * SEGMENT + 256
        for number, (operation, strokes, text) in enumerate(records, 1):
            edit = Edit(offset, 1, operation, strokes, text)
            record = encode_record(edit, number)
            image[offset:offset + len(record)] = record
            offset += len(record)
        answer = f"OK 32 {len(payload)} {zlib.crc32(payload)} " + "0" * 64

        class MemoryConsole:
            received = 0
            def command(self, line, timeout=30):
                prefix = "native_install update read "
                if not line.startswith(prefix):
                    raise AssertionError(line)
                start, size = map(int, line[len(prefix):].split())
                self.received += size
                return "OK " + base64.b64encode(image[start:start + size]).decode()
        return MemoryConsole, answer, image

    def test_empty_journal_reads_one_packet_instead_of_64k(self):
        from native_update import read_snapshot
        Console, answer, _ = self.journal_fixture([])
        full, sparse = Console(), Console()
        expected, _ = read_snapshot(full, answer)
        actual, _ = read_snapshot(sparse, answer, trim_journals=True)
        self.assertEqual(actual.entries, expected.entries)
        self.assertEqual(full.received - sparse.received, 65536 - 512)
        self.assertLess(sparse.received, 1024)

    def test_sparse_journal_matches_full_read_for_cross_packet_records(self):
        from native_update import read_snapshot
        records = [(0, (1,), b"x" * 201), (0, (2,), b"y" * 2047), (2, (1,), b""), (1, (3,), b"")]
        Console, answer, image = self.journal_fixture(records)
        full, sparse = Console(), Console()
        expected, _ = read_snapshot(full, answer)
        actual, _ = read_snapshot(sparse, answer, trim_journals=True)
        self.assertEqual(actual.entries, expected.entries)
        self.assertEqual(actual.highwater, expected.highwater)
        self.assertEqual(actual.used, expected.used)
        self.assertLess(sparse.received, 8192)
        image[2 * 65536 + 256 + 40] ^= 1
        for trim in (False, True):
            with self.subTest(trim=trim), self.assertRaises(ValueError):
                read_snapshot(Console(), answer, trim_journals=trim)


if __name__ == "__main__":
    unittest.main()
