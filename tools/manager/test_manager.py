"""Exercise the Manager's routing against a fake HID console."""

from contextlib import nullcontext
import json
from pathlib import Path
from queue import Queue
import re
from tempfile import TemporaryDirectory
import time
from threading import Thread, current_thread
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import unittest
from unittest.mock import patch

import manager
import import_personal
import manager_repack


class Keyboard:
    serial = "smoke-board"

    def __init__(self):
        self.commands = []
        self.enabled = True
        self.layout = "Windows U.S. (plain text)"
        self.hints = None
        self.kind = "named"
        self.adaptive = False
        self.name = "Medical"
        self.command_threads = []
        self.install_status = "NATIVE2 READY 3"
        self.individual_factory = True

    def command(self, command, timeout=30):
        self.commands.append(command)
        self.command_threads.append(current_thread())
        if command == "native_install status":
            return self.install_status
        if command == "native_catalog":
            return json.dumps([{"id": 8, "name": self.name, "enabled": self.enabled, "kind": self.kind}])
        if command == "user_dictionary_status":
            return '{"format":"SQBCAT02","ready":true,"read_only":false,"tail":64,"generation":3,"auto_checkpoints":2}'
        if command == "stroke_hints":
            return self.hints or "ERR Command unavailable on this keyboard"
        if command in ("stroke_hints auto", "stroke_hints off") and self.hints is not None:
            self.hints = command.split()[1]
            return "OK"
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
        if command == "native_install update factory-member-capabilities":
            return "OK FACTORY2" if self.individual_factory else "ERR Unsupported"
        if command == "native_install update manage-capabilities":
            return "OK MANAGE1"
        if command == "native_install update snapshot-capabilities":
            return "OK SNAPSHOT2" if self.adaptive else "ERR Unsupported"
        if command == "native_install update snapshot-merge-capabilities":
            return "OK MERGE1 16" if self.adaptive else "ERR Unsupported"
        if command == "native_install update personal-snapshot-capabilities":
            return "OK PERSONAL1"
        if command == "native_install update journal-snapshot-capabilities":
            return "OK JOURNAL1"
        if command == "native_install update snapshot-clear-capabilities":
            return "OK CLEAR1" if self.adaptive else "ERR Unsupported"
        if command == "reset_user_dictionary":
            return "OK"
        if command == "native_install update repack-capabilities":
            return "OK REPACK1" if self.adaptive else "ERR Unsupported"
        if command == "native_install update recovery-capabilities":
            return "OK RECOVERY1"
        if command == "native_install update layout-capabilities":
            return "OK MAP1"
        if command == "native_install update layout-map":
            segments = "cc" + "." * 253 + "x"
            return f"OK MAP1 0 w vv {segments} {segments}"
        if command in ("native_install update begin metadata", "native_install update begin backup"):
            return "ERR Unavailable in fixture"
        if command == "native_install update journal-usage-capabilities":
            return "ERR Unsupported"
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

    def test_read_and_save_stroke_hints(self):
        self.assertIsNone(self.run_job("refresh")["state"]["hints"])
        self.keyboard.hints = "auto"
        self.assertEqual(self.run_job("refresh")["state"]["hints"], "auto")
        self.assertEqual(self.run_job("hints", mode="off")["state"]["hints"], "off")
        with self.assertRaisesRegex(ValueError, "Choose Auto or Off"):
            self.run_job("hints", mode=["invalid"])
        self.assertEqual(self.run_job("hints", mode="auto")["state"]["hints"], "auto")

    def test_bluetooth_opener_checks_firmware_identity_not_hid_path(self):
        devices = [dict(path=b"first", bus_type=2), dict(path=b"second", bus_type=2),
                   dict(path=b"wired", bus_type=1)]
        for device in devices:
            device.update(usage_page=0xFF31, usage=0x74)

        class Hid:
            closed = []

            def open_path(self, path):
                self.path = path

            def write(self, packet):
                self.prefix = packet[1:5]
                return len(packet)

            def read(self, size, timeout):
                serial = b"BEEF" if self.path == b"second" else b"ABCD"
                return self.prefix + b"OK SQMANAGER1 " + serial + b"\n\n"

            def close(self):
                self.closed.append(self.path)

        hid = SimpleNamespace(enumerate=lambda: devices, device=Hid)
        with patch.dict("sys.modules", hid=hid):
            with self.assertRaisesRegex(RuntimeError, "connect one"):
                with import_personal.open_bluetooth_console():
                    self.fail("Ambiguous Bluetooth device accepted")
            with import_personal.open_bluetooth_console("BEEF") as console:
                self.assertEqual(console.serial, "BEEF")
                self.assertEqual(console.transport, "bluetooth")
        self.assertEqual(Hid.closed, [b"first", b"second"])

    def test_connect_prefers_usb_and_can_reconnect_over_bluetooth(self):
        jobs = manager.Jobs(None)
        connect = {"action": "refresh", "transport": "auto"}
        with patch.object(manager, "usb_console_devices", side_effect=[[{}], [], [{}]]) as discover_usb, \
                patch.object(manager, "bluetooth_console_devices", return_value=[{}]) as discover_ble, \
                patch.object(manager, "open_bluetooth_console", side_effect=lambda _: nullcontext(self.keyboard)):
            jobs.run(connect)
            self.assertEqual(jobs.snapshot()["result"]["state"]["transport"], "usb")
            discover_ble.assert_not_called()
            self.keyboard.transport = "bluetooth"
            jobs.run(connect)
            self.assertEqual(jobs.snapshot()["result"]["state"]["transport"], "bluetooth")
            self.assertEqual(jobs.connected_serial, self.keyboard.serial)
            self.keyboard.transport = "usb"
            jobs.run(connect)
            self.assertEqual(jobs.snapshot()["result"]["state"]["transport"], "usb")
            discover_usb.assert_called_with(self.keyboard.serial)
            discover_ble.assert_called_once()

    def test_auto_connect_keeps_initial_screen_when_keyboard_is_absent(self):
        jobs = manager.Jobs(None)
        connect = {"action": "refresh", "transport": "auto"}
        with patch.object(manager, "usb_console_devices", return_value=[]), \
                patch.object(manager, "bluetooth_console_devices", return_value=[]):
            jobs.run({**connect, "initial": True})
            self.assertTrue(jobs.snapshot()["result"]["disconnected"])
            jobs.run(connect)
            self.assertEqual(jobs.snapshot()["status"], "error")
            self.assertIn("USB or pair", jobs.snapshot()["message"])

    def test_bluetooth_connection_refresh_and_edits_pin_the_device(self):
        self.keyboard.transport = "bluetooth"
        self.keyboard.adaptive = True
        jobs = manager.Jobs(None)
        with patch.object(manager, "open_bluetooth_console", side_effect=lambda _: nullcontext(self.keyboard)) as opened:
            jobs.run({"action": "refresh", "transport": "bluetooth"})
            state = jobs.snapshot()["result"]["state"]
            self.assertEqual(state["transport"], "bluetooth")
            self.assertFalse(state["repack"])
            self.assertNotIn("native_install update repack-capabilities", self.keyboard.commands)
            jobs.run({"action": "set", "id": 8, "outline": "KAT", "translation": "cat"})
            self.assertEqual(jobs.snapshot()["status"], "done")
            self.assertIn("native_entry commit", self.keyboard.commands)
            self.assertEqual(opened.call_args.args, (self.keyboard.serial,))

    def test_bluetooth_json_import_is_rejected_before_opening_console(self):
        with patch.object(manager, "open_bluetooth_console") as opened:
            for action in ("import", "preview_import", "apply_import"):
                with self.subTest(action=action), self.assertRaisesRegex(ValueError, "Connect over USB to import JSON"):
                    manager.run_job(None, {"action": action}, lambda _: None, transport="bluetooth")
            opened.assert_not_called()

    def test_bluetooth_repack_is_rejected_before_opening_console(self):
        with patch.object(manager, "open_bluetooth_console") as opened:
            for action in ("preview_repack", "apply_repack", "restore_repack_backup"):
                with self.subTest(action=action), self.assertRaisesRegex(ValueError, "Connect over USB"):
                    manager.run_job(None, {"action": action}, lambda _: None, transport="bluetooth")
            opened.assert_not_called()

    def test_bluetooth_setup_reopens_the_same_device_over_bluetooth(self):
        self.keyboard.transport = "bluetooth"
        self.keyboard.install_status = manager.UNPROVISIONED
        request = {"action": "setup", "serial": self.keyboard.serial, "packages": ["lapwing-base"],
                   "approved": True}
        with patch.object(manager, "open_bluetooth_console", return_value=nullcontext(self.keyboard)) as opened, \
                patch.object(manager, "prepare_install_image", return_value=(b"image", {})), \
                patch.object(manager, "provision") as provision, \
                patch.object(manager, "wait_ready", return_value={}) as ready:
            self.assertTrue(manager.run_job(None, request, lambda _: None, transport="bluetooth")["installed"])
            opened.assert_called_once_with(self.keyboard.serial)
            provision.assert_called_once()
            ready.assert_called_once_with(self.keyboard.serial, load_state=unittest.mock.ANY, opener=opened)

    def test_repack_previews_without_backup_then_requires_matching_confirmation(self):
        self.keyboard.adaptive = True
        serial = self.keyboard.serial
        source = {"sha256": "source", "capture": {"serial": serial, "generation": 3}}
        images = [
            {"id": 6, "extent": 1, "start": 13, "segments": 3, "kind": "b", "name": "Lapwing Base"},
            {"id": 8, "extent": 2, "start": 22, "segments": 2, "kind": "s", "name": "Medical"},
        ]
        usage = {8: {"bytes": 48, "count": 1, "entries": 1}}
        before = {"free_segments": 245, "largest_free_run": 200, "staging_blocked": False, "segments": []}
        after = manager_repack.project_layout(images, usage)
        with TemporaryDirectory() as home, patch.object(manager_repack.Path, "home", return_value=Path(home)), \
                patch.object(manager_repack, "backup_all", return_value=source) as backup, \
                patch.object(manager_repack, "read_layout", return_value=before), \
                patch.object(manager_repack, "read_images", return_value=images), \
                patch.object(manager_repack, "overlay_usage", return_value=usage) as measure, \
                patch.object(manager_repack, "build_repack", return_value=(None, {"optimized": True},
                                                                          {"target_layout": after})) as build, \
                patch.object(manager_repack, "restore_repack") as install, \
                patch.object(manager_repack, "recover_usb") as restore, \
                patch.object(manager, "load_view", return_value={"state": {"serial": serial}}):
            result = self.run_job("preview_repack")["repack_plan"]
            measure.assert_called_once_with(self.keyboard, metadata_only=True)
            self.assertEqual(result["before"]["largest_free_run"], 200)
            self.assertEqual(result["after"], after)
            self.assertEqual(len(after["segments"]), 256)
            self.assertEqual("".join(row["kind"] for row in after["segments"][:10]), "ccbbbjssfr")
            backup.assert_not_called()
            build.assert_not_called()
            install.assert_not_called()
            self.assertEqual(list(Path(home).iterdir()), [])
            fields = {"serial": serial, "token": result["token"]}
            with self.assertRaisesRegex(ValueError, "Confirm"):
                self.run_job("apply_repack", **fields, confirm="another-board")
            with self.assertRaisesRegex(ValueError, "Flash changed"):
                self.run_job("apply_repack", **{**fields, "token": "wrong"}, confirm=serial)
            backup.assert_not_called()
            install.assert_not_called()
            self.assertTrue(self.run_job("apply_repack", **fields, confirm=serial)["repacked"])
            backup.assert_called_once_with(self.keyboard, on_progress=unittest.mock.ANY)
            build.assert_called_once_with(source, [])
            saved = manager_repack.last_repack_backup(serial)
            self.assertEqual(json.loads((saved / "original.json").read_text()), source)
            _, target_hash = manager_repack.read_bundle(saved / "optimized.json")
            self.assertEqual(install.call_args.args[3], serial + ":" + target_hash)
            self.assertEqual(install.call_args.kwargs, {"live": True, "show_hashes": False})

            self.keyboard.install_status = "NATIVE2 UNPROVISIONED 3"
            self.assertEqual(manager.read_state(self.keyboard)["setup"]["repack_backup"], saved.name)
            with self.assertRaisesRegex(ValueError, "latest repack backup"):
                self.run_job("restore_repack_backup", serial=serial, confirm=serial, token="1" * 32)
            restore.assert_not_called()
            reply = self.run_job("restore_repack_backup", serial=serial, confirm=serial, token=saved.name)
            self.assertTrue(reply["restored"])
            self.assertEqual(restore.call_args.args[:3], (source, serial, serial))
            self.assertTrue(restore.call_args.kwargs["unprovisioned_only"])

    def test_usb_merge_requires_approval_and_checks_backup_before_install(self):
        self.keyboard.adaptive = True
        serial = self.keyboard.serial
        source = {"capture": {"serial": serial}}
        original = (b"image", unittest.mock.Mock(payload=b"catalog", highwater=3))
        token = manager_repack.offline_merge_token(original[1], serial, 8, 9)
        fields = {"serial": serial, "confirm": serial, "upper": 8, "lower": 9, "token": token}
        with patch.object(manager_repack, "backup_all", return_value=source) as backup, \
                patch.object(manager_repack, "reconstruct", return_value=original), \
                patch.object(manager, "install_offline_merge", return_value="Upper") as install, \
                patch.object(manager, "load_view", return_value={"state": {"serial": serial}}):
            with self.assertRaisesRegex(ValueError, "Connect over USB"):
                manager.run_job(None, {"action": "apply_offline_merge", **fields}, lambda _: None,
                                transport="bluetooth")
            with self.assertRaisesRegex(ValueError, "Confirm"):
                self.run_job("apply_offline_merge", **{**fields, "confirm": "different device"})
            backup.assert_not_called()
            with self.assertRaisesRegex(ValueError, "No erase was started"):
                self.run_job("apply_offline_merge", **{**fields, "token": "0" * 64})
            install.assert_not_called()
            self.assertEqual(self.run_job("apply_offline_merge", **fields)["merged"], "Upper")
            self.assertEqual(install.call_args.args[:2], (source, original))

    def test_repack_preview_with_no_gain_offers_no_rewrite(self):
        self.keyboard.adaptive = True
        layout = {"free_segments": 252, "largest_free_run": 252, "staging_blocked": False}
        with patch.object(manager_repack, "read_layout", return_value=layout), \
                patch.object(manager_repack, "read_images", return_value=[]), \
                patch.object(manager_repack, "overlay_usage", return_value={}), \
                patch.object(manager_repack, "backup_all") as backup:
            result = self.run_job("preview_repack")["repack_plan"]
            self.assertNotIn("token", result)
            backup.assert_not_called()

    def test_unprovisioned_keyboard_offers_setup_without_reading_catalog(self):
        self.keyboard.install_status = manager.UNPROVISIONED
        setup = self.run_job("refresh")["state"]["setup"]
        self.assertEqual(setup, {"serial": self.keyboard.serial, "packages": manager.FACTORY_CHOICES,
                                 "expected_status": manager.UNPROVISIONED, "reprovision": False})
        self.keyboard.individual_factory = False
        with self.assertRaisesRegex(RuntimeError, "did not offer"):
            self.run_job("refresh")
        self.assertNotIn("user_dictionary_status", self.keyboard.commands)
        self.assertNotIn("native_catalog", self.keyboard.commands)
        self.keyboard.install_status += " Incompatible compact snapshot."
        with self.assertRaisesRegex(RuntimeError, "USB recovery"):
            self.run_job("refresh")

    def test_setup_requires_confirmation_and_unprovisioned_target(self):
        fields = {"serial": self.keyboard.serial, "packages": ["plover"]}
        with patch.object(manager, "prepare_install_image", return_value=(b"image", {})) as prepare, \
                patch.object(manager, "provision") as provision, patch.object(manager, "wait_ready") as wait:
            with self.assertRaisesRegex(ValueError, "Confirm"):
                self.run_job("setup", **fields)
            with self.assertRaisesRegex(RuntimeError, "status changed"):
                self.run_job("setup", approved=True, **fields)
            self.keyboard.install_status = manager.UNPROVISIONED
            with self.assertRaisesRegex(ValueError, "serial number"):
                manager.run_job("another-board", dict(action="setup", approved=True, **fields), print)
            prepare.assert_not_called()
            provision.assert_not_called()
            def reconnected(serial, *, load_state):
                self.keyboard.install_status = "NATIVE2 READY 2"
                return load_state(self.keyboard)

            wait.side_effect = reconnected
            self.assertTrue(self.run_job("setup", approved=True, **fields)["installed"])
            prepare.assert_called_once_with(packages=["plover"])
            self.assertEqual(provision.call_args.kwargs["expected_status"], manager.UNPROVISIONED)
            wait.assert_called_once_with(self.keyboard.serial, load_state=unittest.mock.ANY)

    def test_reprovision_only_uses_installer_and_requires_matching_status(self):
        for status in ("NATIVE2 READY 3", "NATIVE2 UNPROVISIONED 7 Incompatible image."):
            with (
                self.subTest(status=status),
                patch.object(manager, "prepare_install_image", return_value=(b"", {})),
                patch.object(manager, "provision") as provision,
                patch.object(manager, "wait_ready", return_value={"state": {"serial": self.keyboard.serial}}),
            ):
                self.keyboard.install_status = status
                self.keyboard.commands.clear()
                setup = self.run_job("open_installer")["state"]["setup"]
                self.assertTrue(setup["reprovision"])
                self.assertEqual(setup["expected_status"], status)
                self.assertNotIn("native_catalog", self.keyboard.commands)
                self.assertNotIn("user_dictionary_status", self.keyboard.commands)
                fields = dict(serial=setup["serial"], packages=["plover"], approved=True, expected_status=status)
                with self.assertRaisesRegex(ValueError, "Confirm the keyboard serial"):
                    self.run_job("setup", **fields)
                self.keyboard.install_status = "NATIVE2 READY 4"
                with self.assertRaisesRegex(RuntimeError, "status changed"):
                    self.run_job("setup", **fields, confirm_serial=setup["serial"])
                provision.assert_not_called()
                self.keyboard.install_status = status
                self.assertTrue(self.run_job("setup", **fields, confirm_serial=setup["serial"])["installed"])
                self.assertEqual(provision.call_args.kwargs["expected_status"], status)
        self.keyboard.install_status = "NATIVE2 INSTALL 3"
        with self.assertRaisesRegex(RuntimeError, "installer is unavailable"):
            self.run_job("open_installer")

    def test_factory_actions_require_individual_firmware(self):
        self.assertEqual(self.run_job("refresh")["state"]["factory_actions"]["4"], "lapwing-uk")
        self.keyboard.individual_factory = False
        self.assertEqual(self.run_job("refresh")["state"]["factory_actions"], {})

    def test_scan_retries_transient_deferral(self):
        layout = {"segments": []}
        with patch.object(manager, "read_layout", side_effect=[
                RuntimeError("ERR Layout scan deferred until the flash and input are idle"), layout
        ]) as read, patch.object(manager.time, "sleep"):
            self.assertEqual(manager.scan_layout(self.keyboard, lambda _: None), layout)
            self.assertEqual(read.call_count, 2)
            self.assertEqual(self.keyboard.commands, ["native_install status"])

    def test_refresh_and_committed_import_include_memory_map(self):
        self.keyboard.adaptive = True
        request = {"source": '{"KAT":"cat"}', "name": "Medical", "token": "a" * 64}
        layout = {"segments": []}
        with patch.object(manager, "load_layout", return_value=layout) as scan, \
                patch.object(manager, "named_import") as publish, \
                patch.object(manager, "wait_ready", side_effect=lambda serial, *, load_state, checking:
                             load_state(self.keyboard)) as wait:
            self.assertEqual(self.run_job("refresh")["layout_map"], layout)
            self.run_job("preview_import", **request)
            self.assertEqual(scan.call_count, 1)
            result = self.run_job("apply_import", **request)
            self.assertEqual(result["imported"], 1)
            self.assertEqual(result["layout_map"], layout)
            self.assertEqual(scan.call_count, 2)
            scan.side_effect = TimeoutError("Map timed out")
            result = self.run_job("apply_import", **request)
            self.assertEqual(result["imported"], 1)
            self.assertEqual(result["layout_map_error"], "Map timed out")
            wait.side_effect = RuntimeError("Reconnect timed out")
            self.assertEqual(self.run_job("apply_import", **request), {"commit_pending": True})
            self.assertTrue(publish.call_args.kwargs["stage"])

    def test_factory_package_requires_preview(self):
        with patch.object(manager, "factory_package", return_value={"package": "lapwing"}) as change, patch.object(manager, "wait_ready") as wait:
            self.assertEqual(self.run_job("refresh")["state"]["factory_actions"]["6"], "lapwing-base")
            with self.assertRaisesRegex(ValueError, "Preview"):
                self.run_job("apply_package", package="lapwing-base", restore=False)
            change.assert_not_called()
            self.assertIn("package_plan", self.run_job("preview_package", package="lapwing-base", restore=False))
            self.assertIsNone(change.call_args.kwargs["confirmation"])
            wait.assert_not_called()
            self.assertIn("package_changed", self.run_job(
                "apply_package", package="lapwing-base", restore=False, token="a" * 64))
            wait.assert_called_once_with(self.keyboard.serial, load_state=unittest.mock.ANY, checking=unittest.mock.ANY)
        self.keyboard.kind = "uninstalled"
        with self.assertRaisesRegex(ValueError, "Install the factory dictionary"):
            self.run_job("enable", id=8, enabled=True)

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
            wait.assert_called_once_with("smoke-board", load_state=unittest.mock.ANY, checking=unittest.mock.ANY)


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

    def test_refresh_measures_journal_usage(self):
        usage = {8: {"count": 2, "entries": 1, "bytes": 92}}
        with patch.object(manager, "overlay_usage", return_value=usage) as inspect:
            self.assertEqual(self.run_job("refresh")["usage"], usage)
            self.assertEqual(inspect.call_count, 1)
            inspect.reset_mock()
            self.assertNotIn("usage", self.run_job("enable", id=8, enabled=False))
            inspect.assert_not_called()

    def test_map_only_attributes_journal_blocks_from_the_scanned_generation(self):
        self.keyboard.adaptive = True
        layout = {"segments": []}
        usage = {8: {"count": 2, "entries": 1, "bytes": 92, "blocks": {4: 92}}}
        with patch.object(manager, "load_layout", side_effect=lambda *_: dict(layout)), \
                patch.object(manager, "overlay_usage", return_value=usage):
            self.assertEqual(self.run_job("layout_map")["layout_map"]["journal_usage"], usage)
            self.keyboard.install_status = "NATIVE2 READY 4"
            self.assertNotIn("journal_usage", self.run_job("layout_map")["layout_map"])

    def test_clear_personal_requires_confirmation_and_firmware_capability(self):
        with patch.object(manager, "clear_personal", return_value=True) as clear, patch.object(manager, "wait_ready"):
            with self.assertRaisesRegex(ValueError, "personal dictionary"):
                self.run_job("clear_personal", id=8, confirm="CLEAR")
            self.keyboard.name, self.keyboard.kind = "user_dictionary", "snapshot"
            with self.assertRaisesRegex(ValueError, "type CLEAR"):
                self.run_job("clear_personal", id=8, confirm="NO")
            with self.assertRaisesRegex(RuntimeError, "Flash the current left firmware"):
                self.run_job("clear_personal", id=8, confirm="CLEAR")
            clear.assert_not_called()
            self.keyboard.adaptive = True
            self.assertTrue(self.run_job("clear_personal", id=8, confirm="CLEAR")["cleared"])
            clear.assert_called_once_with(self.keyboard, 8, unittest.mock.ANY)
            self.keyboard.kind = "personal"
            clear.reset_mock()
            self.assertTrue(self.run_job("clear_personal", id=8, confirm="CLEAR")["cleared"])
            clear.assert_called_once_with(self.keyboard, 8, unittest.mock.ANY)

    def test_connection_probe_detects_removed_board(self):
        jobs = manager.Jobs(self.keyboard.serial)
        jobs.connected_serial = self.keyboard.serial
        with patch.object(manager, "usb_console_devices", return_value=[{}]):
            jobs.check_connection()
        self.assertEqual(jobs.connected_serial, self.keyboard.serial)
        self.assertEqual(self.keyboard.commands, [])
        jobs.next_connection_check = 0
        with patch.object(manager, "usb_console_devices", return_value=[]):
            jobs.check_connection()
        self.assertIsNone(jobs.connected_serial)
        self.assertTrue(jobs.snapshot()["connection_lost"])

    def test_compact_merge_previews_and_requires_usb_and_approval(self):
        self.keyboard.adaptive = True
        self.assertEqual(self.run_job("refresh")["state"]["merge_limit"], 16)
        with self.assertRaisesRegex(ValueError, "Connect over USB"):
            manager.run_job(None, {"action": "preview_merge"}, lambda _: None, transport="bluetooth")
        with patch.object(manager, "merge_snapshots", return_value={"token": "a" * 64}) as merge:
            self.assertEqual(self.run_job("preview_merge", upper=8, lower=9)["merge_plan"]["token"], "a" * 64)
            self.assertEqual(merge.call_args.args[1:], (8, 9))
            with self.assertRaisesRegex(ValueError, "Preview the merge"):
                self.run_job("apply_merge", upper=8, lower=9)
            merge.assert_called_once()
        with patch.object(manager, "merge_snapshots", return_value="Upper") as merge, \
                patch.object(manager, "wait_ready", return_value={"state": {}}):
            self.assertEqual(self.run_job("apply_merge", upper=8, lower=9, token="a" * 64)["merged"], "Upper")
            self.assertEqual(merge.call_args.kwargs["expected"], "a" * 64)

    def test_named_journal_can_preview_conversion(self):
        self.keyboard.adaptive = True
        self.assertTrue(self.run_job("refresh")["state"]["journal_conversion"])
        with patch.object(manager, "change_snapshot", return_value={"token": "a" * 64}) as change:
            self.assertIn("optimization_plan", self.run_job("preview_optimize", id=8, operation="convert"))
            change.assert_called_once_with(self.keyboard, "convert", "Medical", stage=False,
                                           preview=True, expected=None, target_id=8, progress=unittest.mock.ANY)

    def test_optimize_requires_matching_selection_and_preview(self):
        self.keyboard.adaptive = True
        self.keyboard.kind = "snapshot"
        self.assertTrue(self.run_job("refresh")["state"]["personal_conversion"])
        with patch.object(manager, "change_snapshot") as change, patch.object(manager, "wait_ready") as wait:
            with self.assertRaisesRegex(ValueError, "Preview"):
                self.run_job("apply_optimize", id=8, operation="consolidate")
            with self.assertRaisesRegex(ValueError, "eligible"):
                self.run_job("preview_optimize", id=8, operation="personal")
            with self.assertRaisesRegex(ValueError, "factory dictionaries"):
                self.run_job("reset", id=8, outline="KAT")
            with self.assertRaisesRegex(ValueError, "eligible"):
                self.run_job("preview_optimize", id=8, operation="replace", source='{"KAT":"new cat"}')
            change.assert_not_called()
            plan = {"id": 8, "operation": "consolidate", "token": "a" * 64}
            change.return_value = plan
            self.assertEqual(self.run_job("preview_optimize", id=8, operation="consolidate")["optimization_plan"], plan)
            self.assertTrue(change.call_args.kwargs["preview"])
            self.assertFalse(change.call_args.kwargs["stage"])
            wait.assert_not_called()
            change.return_value = True
            reply = self.run_job("apply_optimize", id=8, operation="consolidate", token=plan["token"])
            self.assertEqual(reply["optimized"], "Medical")
            self.assertEqual(change.call_args.kwargs["target_id"], 8)
            self.assertEqual(change.call_args.kwargs["expected"], plan["token"])
            wait.assert_called_once_with("smoke-board", load_state=unittest.mock.ANY, checking=unittest.mock.ANY)

    def test_import_explains_empty_and_invalid_files(self):
        self.keyboard.kind = "snapshot"
        with patch.object(manager, "named_import") as named, patch.object(manager, "transfer") as transfer:
            for action, fields in (
                ("preview_import", {"name": "Abby"}),
                ("import", {}),
            ):
                with self.assertRaisesRegex(ValueError, "zero entries"):
                    self.run_job(action, source="{}\n", **fields)
            with self.assertRaisesRegex(ValueError, "not valid JSON"):
                self.run_job("preview_import", name="Abby", source="{")
            named.assert_not_called()
            transfer.assert_not_called()
        with patch.object(manager, "named_import", return_value={}) as named:
            self.run_job("preview_import", name="Medical", source='{"KAT":"cat"}')
            self.assertEqual(named.call_args.args[1], "Medical")
            self.assertFalse(named.call_args.kwargs["stage"])
        with patch.object(manager, "transfer", return_value=False) as transfer:
            with self.assertRaisesRegex(ValueError, "Preview named imports"):
                self.run_job("import", name="Medical", source='{"KAT":"cat"}')
            transfer.assert_not_called()
            self.run_job("import", source='{"KAT":"cat"}')
            transfer.assert_called_once()
            self.assertFalse(transfer.call_args.kwargs)

    def test_backup_always_returns_a_complete_bundle(self):
        with patch.object(manager, "backup_all", return_value={"format": "stenosquid-backup-2"}) as backup:
            self.assertEqual(self.run_job("backup")["filename"], "stenosquid-backup.json")
            backup.assert_called_with(self.keyboard)

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
