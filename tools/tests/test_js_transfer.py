#!/usr/bin/env python3
"""The JavaScript transfer client against the real firmware logic (device simulator), clean and lossy.

Same device and acceptance rule as test_native_transfer.py: after the upload the simulator compares its flash with the
legacy erase/write/commit result. Also checks the JS record stream is byte-identical to the Python one.
Skipped when Node is not installed. Run in a tree where tools/tests/test_native_transfer.py works (it supplies the
simulator build and the fixtures).
"""
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
for sub in ("tools/tests", "tools/storage", "tools/dictionaries", "tools/dict_compiler"):
    sys.path.insert(0, str(ROOT / sub))

import native_transfer as nt
from test_native_transfer import Fixture

BROWSER = ROOT / "tools/manager/browser"


@unittest.skipUnless(shutil.which("node"), "Node is not installed")
class JavaScriptScriptedTests(unittest.TestCase):
    """The browser-side codec and the scripted-keyboard protocol cases (no simulator needed)."""

    def run_node(self, name):
        result = subprocess.run(["node", str(BROWSER / name)], capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_frame_codec(self):
        self.run_node("test_transfer_frame.mjs")

    def test_scripted_client_protocol_cases(self):
        self.run_node("test_transfer_client.mjs")

    def test_snapshot_reader_matches_python_golden_vectors(self):
        """Manager milestone M1: the JS catalog/journal reader and metadata planner agree with the Python oracle."""
        self.run_node("test_snapshot.mjs")

    def test_console_snapshot_reads_match_python(self):
        self.run_node("test_device_reads.mjs")

    def test_update_planner_is_byte_identical_to_python(self):
        """Manager milestone M2: the JS copy-on-write planner produces the same segments and catalog as plan()."""
        self.run_node("test_plan.mjs")

    def test_snapshot_and_import_planners_are_byte_identical_to_python(self):
        """Manager milestone M2: named snapshots, change/merge, import cost and the token digests."""
        self.run_node("test_snapshot_plan.mjs")

    def test_factory_package_planner_is_byte_identical_to_python(self):
        """Manager milestone M2: installing and uninstalling the built-in dictionaries, report and confirmation included."""
        self.run_node("test_package_plan.mjs")

    def test_lookup_reader_and_base_replacement_match_python(self):
        """Manager milestones M1/M2: the JSC4 lookup reader, and replacing a pinned base with conflict detection."""
        self.run_node("test_replace.mjs")

    def test_dictionary_compiler_is_byte_identical_to_python(self):
        """Manager milestones M1/M2: the JSC4 compiler and compile_snapshot, including pointer lists and refusals."""
        self.run_node("test_compile.mjs")

    def test_plover_json_importer_matches_python(self):
        """Manager milestone M1: ordered JSON import (duplicate keys, limits, refusals) and the export file bytes."""
        self.run_node("test_import.mjs")

    def test_flash_map_and_image_validation_match_python(self):
        """Manager milestone M3: flash map parsing, paged scans, image metadata reads and compact-layout validation."""
        self.run_node("test_layout.mjs")

    def test_vendor_console_matches_python(self):
        """Manager milestone M3: report framing, replies, retries on flash busy and timeouts match Python's Console."""
        self.run_node("test_console.mjs")

    def test_keyboard_state_reads_match_python(self):
        """Manager milestone M3: read_state, load_view, installer_view and overlay_usage ask and conclude as manager.py does."""
        self.run_node("test_state.mjs")

    def test_job_orchestration_matches_python(self):
        """Manager milestone M3: run_job's checks, session handling, ordering and waits match manager.py action by action."""
        self.run_node("test_jobs.mjs")

    def test_publish_and_transfer_match_python(self):
        """Manager milestone M3: transfer() (move, rename, delete, import, compact) publishes through the same console commands."""
        self.run_node("test_transfer.mjs")

    def test_dictionary_export_matches_python(self):
        """Manager milestone M3: export_dictionary sends the same console commands and returns the same entries."""
        self.run_node("test_export.mjs")

    def test_recovery_bundles_match_python(self):
        """Manager milestone M3: capturing, signing and validating a complete backup agree with native_recovery.py, damaged ones included."""
        self.run_node("test_recovery.mjs")

    def test_backup_matches_python(self):
        """Manager milestone M3: backup_all reads, stamps and aborts exactly as native_update.backup_all does."""
        self.run_node("test_backup.mjs")

    def test_dictionary_editor_matches_python(self):
        """Manager milestone M3: read_document / change_document sessions (cache, previews, saves) match manager_editor.py."""
        self.run_node("test_editor.mjs")

    def test_bundled_factory_dictionaries(self):
        """The editor's bundled-factory lookup (zip reader, digest rules) behaves like manager_editor.factory_source."""
        self.run_node("test_factory.mjs")

    def test_page_bundle_runs_like_python(self):
        """The offline page's bundled modules load, route requests and refresh through the console like the Python Manager."""
        import tempfile
        sys.path.insert(0, str(ROOT / "tools/ci"))
        from bundle_modules import bundle
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "page_bundle.js"
            path.write_text(bundle(BROWSER / "backend.js"), encoding="utf-8")
            result = subprocess.run(["node", str(BROWSER / "test_page_bundle.mjs"), str(path)], capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_dictionary_decoder_matches_python(self):
        """Manager milestone M1: decoding, validation and effective entries agree with the Python reader."""
        self.run_node("test_decode.mjs")

    def test_golden_vectors_are_current(self):
        for script in ("golden_snapshot.py", "golden_plan.py", "golden_decode.py", "golden_snapshot_plan.py", "golden_package.py", "golden_replace.py", "golden_compile.py", "golden_import.py", "golden_layout.py", "golden_console.py", "golden_state.py", "golden_jobs.py", "golden_export.py", "golden_transfer.py", "golden_recovery.py", "golden_backup.py", "golden_editor.py"):
            with self.subTest(script=script):
                result = subprocess.run([sys.executable, str(BROWSER / script), "--check"],
                                        capture_output=True, text=True, timeout=900)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


@unittest.skipUnless(shutil.which("node"), "Node is not installed")
class JavaScriptTransferTests(Fixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build()

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def run_js(self, name, *faults):
        update, kind = self.plans[name]
        spec = self.path / f"{name}.update.json"
        spec.write_text(json.dumps({
            "segments": [[segment, block.hex()] for segment, block in update.segments.items()],
            "metadata": update.metadata, "payload": update.payload.hex()}))
        result = subprocess.run(
            ["node", str(BROWSER / "test_transfer_sim.mjs"), str(self.binary), str(self.before),
             str(self.path / f"{name}.bin"), str(kind), str(spec), *map(str, faults)],
            capture_output=True, text=True, timeout=300)
        self.assertTrue(result.stdout.strip(), result.stderr)
        return update, json.loads(result.stdout.strip().splitlines()[-1]), result

    def test_clean_upload_matches_legacy_flash(self):
        for name in ("import", "compact"):
            with self.subTest(name):
                update, out, result = self.run_js(name)
                self.assertEqual(result.returncode, 0, out)
                self.assertRegex(out["verdict"], r"^MATCH \d+$")
                self.assertEqual(out["messages"][0], "Writing dictionary data… 0%")
                self.assertTrue(any("100%" in m for m in out["messages"]))

    def test_record_stream_is_byte_identical_to_python(self):
        for name in ("import", "compact"):
            with self.subTest(name):
                update, out, _ = self.run_js(name)
                self.assertEqual(out["recordsSha256"], hashlib.sha256(nt.records(update)).hexdigest())

    def test_lossy_link_still_matches_or_fails_loudly(self):
        for seed in range(1, 9):
            with self.subTest(seed=seed):
                _, out, result = self.run_js("import", 0.06, 0.04, 0.05, 0.3, seed)
                self.assertEqual(result.returncode, 0, out)
                self.assertRegex(out["verdict"], r"^MATCH \d+$")


if __name__ == "__main__":
    unittest.main()
