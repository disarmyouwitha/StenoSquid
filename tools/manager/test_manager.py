"""Exercise the Manager's routing against a fake USB console."""

from contextlib import nullcontext
import json
import unittest
from unittest.mock import patch

import manager


class Keyboard:
    def __init__(self):
        self.commands = []
        self.enabled = True
        self.layout = "Windows U.S. (plain text)"

    def command(self, command, timeout=30):
        self.commands.append(command)
        if command == "native_catalog":
            return json.dumps([{"id": 8, "name": "Medical", "enabled": self.enabled, "kind": "named"}])
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
        result = self.run_job("export", id=8)
        self.assertEqual(result["download"], {"KAT": "ça"})
        with patch.object(manager, "transfer", return_value=True) as transfer, patch.object(manager, "wait_ready") as wait:
            with self.assertRaises(ValueError):
                self.run_job("delete", id=8, confirm="other")
            transfer.assert_not_called()
            self.run_job("delete", id=8, confirm="Medical")
            self.assertEqual(transfer.call_args.kwargs, {"delete": "Medical"})
            wait.assert_called_once()


if __name__ == "__main__":
    unittest.main()
