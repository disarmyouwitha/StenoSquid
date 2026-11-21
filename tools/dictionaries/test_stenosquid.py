"""Exercise the USB HID installer and diagnostics CLI without hardware."""
from contextlib import nullcontext, redirect_stdout, redirect_stderr
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import stenosquid
import import_personal


class InstallerTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_storage_retries_only_when_firmware_reports_busy_before_dispatch(self):
        console = import_personal.Console(mock.Mock())
        with mock.patch.object(console, "_exchange", side_effect=[
                "ERR Native flash busy", "OK", TimeoutError("lost acknowledgment")
        ]) as exchange, mock.patch.object(import_personal.time, "sleep"):
            self.assertEqual(console.command("native_install erase 2"), "OK")
            self.assertEqual(exchange.call_count, 2)
            self.assertEqual(exchange.call_args_list[0], exchange.call_args_list[1])
            with self.assertRaisesRegex(TimeoutError, "lost acknowledgment"):
                console.command("native_install erase 3")
            self.assertEqual(exchange.call_count, 3)

    def test_native_install_skips_ready_store_and_refuses_unconfirmed_erase(self):
        for status in ('NATIVE2 READY 3', 'NATIVE2 UNPROVISIONED 0'):
            with self.subTest(status=status):
                console = mock.Mock()
                console.command.return_value = status
                with mock.patch.object(import_personal, 'open_console', return_value=nullcontext(console)), \
                     mock.patch.object(sys, 'argv', ['stenosquid.py', '--install']), redirect_stdout(io.StringIO()):
                    if 'UNPROVISIONED' in status:
                        with self.assertRaisesRegex(RuntimeError, '--confirm-erase'):
                            stenosquid.main()
                    else:
                        stenosquid.main()
                console.command.assert_called_once_with('native_install status')

    def test_packed_install_refuses_older_native_protocol(self):
        console = mock.Mock()
        console.command.return_value = 'NATIVE1 READY 3'
        with mock.patch.object(import_personal, 'open_console', return_value=nullcontext(console)), \
             mock.patch.object(sys, 'argv', ['stenosquid.py', '--install', '--confirm-erase']), \
             self.assertRaisesRegex(RuntimeError, 'matching Native firmware'):
            stenosquid.main()
        console.command.assert_called_once_with('native_install status')

    def test_named_import_and_priority_move_use_staged_protocol(self):
        source = self.root / 'medical.json'
        source.write_text('{"KAT": "triage"}')
        sys.path.insert(0, str(stenosquid.REPO / "tools/storage"))
        for args, function, options in (
            (['--import', str(source), '--as', 'Medical'], 'native_import_plan.named_import', {'stage': True}),
            (['--move', 'Medical', '--position', '1'], 'native_update.transfer', {'move': ('Medical', 1)}),
        ):
            with self.subTest(args=args):
                console = mock.Mock()
                console.command.side_effect = [
                    'NATIVE2 READY 3', '{"ready":true,"read_only":false,"personal_id":1}',
                ]
                with mock.patch.object(import_personal, 'open_console', return_value=nullcontext(console)), \
                     mock.patch.object(sys, 'argv', ['stenosquid.py', *args]), \
                     mock.patch(function, return_value=True) as transfer, \
                     mock.patch('native_install.wait_ready') as wait:
                    stenosquid.main()
                self.assertEqual(transfer.call_args.kwargs, options)
                wait.assert_called_once_with(None)

    def test_diag_auto_detects_one_port(self):
        port = object()
        with mock.patch.object(stenosquid, 'find_port', return_value='COM1'), \
             mock.patch.object(stenosquid, 'Serial', return_value=nullcontext(port)), \
             mock.patch.object(stenosquid, 'diag') as diag, \
             mock.patch.object(sys, 'argv', ['stenosquid.py', '--diag']), redirect_stdout(io.StringIO()):
            stenosquid.main()
        self.assertEqual(diag.call_args_list, [mock.call(port, section) for section in ('status', 'log', 'rail', 'bench')])

    def test_manager_is_default_and_accepts_serial_or_explicit_flags(self):
        manager = mock.Mock()
        with mock.patch.dict(sys.modules, {'manager': manager}), mock.patch.object(sys, 'path', sys.path.copy()):
            for args, serial in (([], None), (['--serial', 'left123'], 'left123'),
                                 (['--manager'], None), (['--manage'], None)):
                with self.subTest(args=args), mock.patch.object(sys, 'argv', ['stenosquid.py', *args]):
                    manager.serve.reset_mock()
                    stenosquid.main()
                    manager.serve.assert_called_once_with(serial)

    def test_serial_provisioning_and_multiple_actions_are_rejected(self):
        for args in (['COM1', '--install'], ['--repair', '--verify'], ['--backup', 'a.json', '--status'],
                     ['--manage', '--status'], ['--confirm-erase'], ['--position', '1']):
            with mock.patch.object(sys, 'argv', ['stenosquid.py', *args]), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
                stenosquid.main()
            self.assertEqual(result.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
