"""Exercise the Manager's routing against a fake USB console."""

from contextlib import nullcontext
import json
from queue import Queue
import re
import time
from threading import Thread, current_thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import unittest
from unittest.mock import patch

import manager


class Keyboard:
    serial = "smoke-board"

    def __init__(self):
        self.commands = []
        self.enabled = True
        self.layout = "Windows U.S. (plain text)"
        self.kind = "named"
        self.adaptive = False
        self.name = "Medical"
        self.command_threads = []

    def command(self, command, timeout=30):
        self.commands.append(command)
        self.command_threads.append(current_thread())
        if command == "native_catalog":
            return json.dumps([{"id": 8, "name": self.name, "enabled": self.enabled, "kind": self.kind}])
        if command == "user_dictionary_status":
            return '{"format":"SQBCAT02","ready":true,"read_only":false,"tail":64,"auto_checkpoints":2}'
        if command == "get_parameter host_layout":
            return self.layout
        if command == "get_parameter available_host_layouts":
            return json.dumps(["Mac U.S.", self.layout])
        if command == "set_host_layout Mac U.S.":
            self.layout = "Mac U.S."
            return "OK"
        if command == "disable_dictionary Medical":
            self.enabled = False
            return "OK"
        if command == "print_dictionary Medical":
            return '{"KAT":"ça"}'
        if command == "native_install update manage-capabilities":
            return "OK MANAGE1"
        if command == "native_install update snapshot-capabilities":
            return "OK SNAPSHOT2" if self.adaptive else "ERR Unsupported"
        if command == "native_install update personal-snapshot-capabilities":
            return "OK PERSONAL1"
        if command == "native_install update layout-capabilities":
            return "OK MAP1"
        if command.startswith("native_entry "):
            return "OK"
        raise AssertionError(command)


class ManagerTest(unittest.TestCase):
    def setUp(self):
        self.keyboard = Keyboard()
        opened = patch.object(manager, "open_console", side_effect=lambda _: nullcontext(self.keyboard))
        opened.start()
        self.addCleanup(opened.stop)

    def run_job(self, action, **fields):
        return manager.run_job(None, dict(action=action, **fields), lambda _: None)

    def test_refresh_and_checkbox(self):
        self.assertEqual(self.run_job("refresh")["state"]["auto_checkpoints"], 2)
        self.assertFalse(self.run_job("enable", id=8, enabled=False)["state"]["catalog"][0]["enabled"])
        self.assertEqual(self.run_job("layout", layout="Mac U.S.")["state"]["layout"], "Mac U.S.")

    def test_edit_uses_selected_stable_id(self):
        self.run_job("set", id=8, outline="KAT", translation="ça\nva")
        self.assertIn("native_entry begin 8 6 KAT", self.keyboard.commands)
        with self.assertRaises(ValueError):
            self.run_job("remove", id=9, outline="KAT")
        with self.assertRaises(ValueError):
            self.run_job("set", id=8, outline="KAT\nnative_entry abort", translation="bad")

    def test_export_and_delete_confirmation(self):
        with patch.object(manager, "export_dictionary", return_value={"KAT": "ça"}) as export:
            result = self.run_job("export", id=8)
            self.assertEqual(result["download"], {"KAT": "ça"})
            export.assert_called_once_with(self.keyboard, "Medical")
        with patch.object(manager, "transfer", return_value=True) as transfer, patch.object(manager, "wait_ready") as wait:
            with self.assertRaises(ValueError):
                self.run_job("delete", id=8, confirm="other")
            transfer.assert_not_called()
            self.run_job("delete", id=8, confirm="Medical")
            self.assertEqual(transfer.call_args.kwargs, {"delete": "Medical"})
            wait.assert_called_once_with("smoke-board")


    def test_rename_and_delete_named_and_protect_builtin_rows(self):
        with patch.object(manager, "transfer", return_value=True) as transfer, \
                patch.object(manager, "change_snapshot", return_value=True) as change, \
                patch.object(manager, "wait_ready"):
            self.run_job("rename", id=8, name="Anatomy")
            self.assertEqual(transfer.call_args.kwargs, {"rename": ("Medical", "Anatomy")})
            self.keyboard.kind = "snapshot"
            self.run_job("delete", id=8, confirm="Medical")
            change.assert_called_once_with(self.keyboard, "delete", "Medical", stage=True, progress=unittest.mock.ANY)
            transfer.reset_mock()
            change.reset_mock()
            for name, kind in (("Lapwing Base", "base"), ("user_dictionary", "snapshot")):
                self.keyboard.name, self.keyboard.kind = name, kind
                with self.assertRaisesRegex(ValueError, "added dictionaries"):
                    self.run_job("rename", id=8, name="Anatomy")
                with self.assertRaisesRegex(ValueError, "added dictionaries"):
                    self.run_job("delete", id=8, confirm=name)
            transfer.assert_not_called()
            change.assert_not_called()

    def test_measure_counts_saved_edits(self):
        usage = {8: {"count": 2, "bytes": 92}}
        with patch.object(manager, "overlay_usage", return_value=usage) as inspect:
            self.assertEqual(self.run_job("measure")["usage"], usage)
            inspect.assert_called_once_with(self.keyboard)

    def test_optimize_requires_matching_selection_and_preview(self):
        self.keyboard.adaptive = True
        self.keyboard.kind = "snapshot"
        self.assertTrue(self.run_job("refresh")["state"]["personal_conversion"])
        with patch.object(manager, "change_snapshot") as change, patch.object(manager, "wait_ready") as wait:
            with self.assertRaisesRegex(ValueError, "Preview"):
                self.run_job("apply_optimize", id=8, operation="consolidate")
            with self.assertRaisesRegex(ValueError, "eligible"):
                self.run_job("preview_optimize", id=8, operation="personal")
            with self.assertRaisesRegex(ValueError, "pinned"):
                self.run_job("reset", id=8, outline="KAT")
            change.assert_not_called()
            plan = {"id": 8, "operation": "consolidate", "token": "a" * 64}
            change.return_value = plan
            self.assertEqual(self.run_job("preview_optimize", id=8, operation="consolidate")["optimization_plan"], plan)
            self.assertTrue(change.call_args.kwargs["preview"])
            self.assertFalse(change.call_args.kwargs["stage"])
            wait.assert_not_called()
            change.return_value = True
            reply = self.run_job("apply_optimize", id=8, operation="replace", token=plan["token"],
                                 source='{"KAT":"replacement"}')
            self.assertEqual(reply["optimized"], "Medical")
            self.assertEqual(change.call_args.kwargs["target_id"], 8)
            self.assertEqual(change.call_args.kwargs["expected"], plan["token"])
            self.assertEqual(change.call_args.kwargs["source"].data[-12:-8], b"SHI1")
            wait.assert_called_once_with("smoke-board")

    def test_import_explains_empty_and_invalid_files(self):
        self.keyboard.kind = "snapshot"
        with patch.object(manager, "named_import") as named, patch.object(manager, "transfer") as transfer:
            for action, fields in (
                ("preview_import", {"name": "Abby"}),
                ("import", {}),
                ("preview_optimize", {"id": 8, "operation": "replace"}),
            ):
                with self.assertRaisesRegex(ValueError, "zero entries"):
                    self.run_job(action, source="{}\n", **fields)
            with self.assertRaisesRegex(ValueError, "not valid JSON"):
                self.run_job("preview_import", name="Abby", source="{")
            named.assert_not_called()
            transfer.assert_not_called()
        with patch.object(manager, "named_import", return_value={}) as named:
            self.run_job("preview_import", name="Medical", source='{"KAT":"cat"}')
            self.assertEqual(named.call_args.kwargs["preference"], "auto")

    def test_complete_backup_has_separate_action(self):
        with patch.object(manager, "backup_all", return_value={"format": "stenosquid-backup-2"}) as backup:
            self.assertEqual(self.run_job("backup")["filename"], "stenosquid-backup.json")
            backup.assert_called_with(self.keyboard, complete=False)
            self.assertEqual(self.run_job("backup", complete=True)["filename"], "stenosquid-rescue.json")
            backup.assert_called_with(self.keyboard, complete=True)

    def test_local_http_routes_require_session_and_origin(self):
        started = Queue()
        server_type = manager.ThreadingHTTPServer
        make_server = patch.object(manager, "ThreadingHTTPServer",
                                   side_effect=lambda *args: _publish(server_type(*args), started))
        with make_server, patch.object(manager.webbrowser, "open", side_effect=started.put):
            server_thread = Thread(target=manager.serve, daemon=True)
            server_thread.start()
            server, url = started.get(timeout=2), started.get(timeout=2)
            try:
                with urlopen(url, timeout=2) as response:
                    html = response.read()
                token = re.search(rb'name="manager-token" content="([^"]+)"', html).group(1).decode()
                with self.assertRaises(HTTPError) as unauthorized:
                    urlopen(url + "/api/job", timeout=2)
                self.assertEqual(unauthorized.exception.code, 403)
                headers = {"X-StenoSquid-Token": token, "Content-Type": "application/json"}
                task = b'{"action":"refresh"}'
                with self.assertRaises(HTTPError) as wrong_origin:
                    urlopen(Request(url + "/api/job", data=task, headers={**headers, "Origin": "https://example.org"}),
                            timeout=2)
                self.assertEqual(wrong_origin.exception.code, 403)
                with urlopen(Request(url + "/api/job", data=task, headers={**headers, "Origin": url}), timeout=2) as response:
                    self.assertEqual(response.status, 202)
                    identity = json.load(response)["id"]
                headers["Origin"] = url
                deadline = time.monotonic() + 2
                while True:
                    with urlopen(Request(url + "/api/job", headers=headers), timeout=2) as response:
                        job = json.load(response)
                    self.assertEqual(job["id"], identity)
                    if job["status"] != "running" or time.monotonic() >= deadline:
                        break
                    time.sleep(0.01)
                self.assertEqual(job["status"], "done")
                self.assertTrue(self.keyboard.command_threads)
                self.assertEqual(set(self.keyboard.command_threads), {server_thread})
            finally:
                server.shutdown()
                server_thread.join(timeout=2)


def _publish(server, started):
    started.put(server)
    return server


if __name__ == "__main__":
    unittest.main()
