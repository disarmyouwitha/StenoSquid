"""Exercise the USB HID installer and diagnostics CLI without hardware."""
import base64
from contextlib import nullcontext, redirect_stdout, redirect_stderr
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import stenosquid
import import_personal


class Keyboard:
    def __init__(self, images, verified=(), corrupt=()):
        self.images = images
        self.verified = set(verified)
        self.corrupt = set(corrupt)
        self.commands = []
        self.buffers = {}
        self.active = None
        self.failures = 0
        self.drop_ack = False
        self.conflict = False
        self.resume = 0

    def command(self, text, timeout=30):
        self.commands.append(text)
        if text == 'native_install status':
            return 'ERR Command unavailable on this keyboard'
        _, operation, *args = text.split()
        if operation == 'status':
            state = 'READY' if len(self.verified) == len(self.images) else 'INSTALL'
            return '\n'.join([f'2 {state}'] + [f'{name} {len(data)} {zlib.crc32(data)} {int(name in self.verified)}'
                for name, data in self.images.items()])
        if operation == 'verify':
            name, = args
            return 'MISMATCH 0' if name in self.corrupt else f'OK {zlib.crc32(self.images[name])}'
        if operation in ('begin', 'repair'):
            self.active, = args
            self.buffers[self.active] = bytearray(self.images[self.active][:self.resume])
            return f'OK {self.resume}'
        if operation == 'data':
            if self.conflict: return 'ERR Base region holds different data. No sectors were erased'
            if self.failures:
                self.failures -= 1
                raise TimeoutError('lost USB response')
            offset, encoded = args
            data = base64.b64decode(encoded, validate=True)
            received = self.buffers[self.active]
            if int(offset) == len(received): received.extend(data)
            else: assert received[int(offset):int(offset)+len(data)] == data
            if self.drop_ack:
                self.drop_ack = False
                raise TimeoutError('ACK lost after writing')
            return f'OK {len(received)}'
        if operation == 'commit':
            assert self.buffers[self.active] == self.images[self.active]
            self.verified.add(self.active)
            self.corrupt.discard(self.active)
            return 'OK'
        if operation == 'abort': return 'OK'
        raise AssertionError(text)


class InstallerTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.images = {name: b'JSC4' + bytes([i]) * 1196 for i, name in enumerate(('LAPWING', 'PLOVER', 'EXTRAS'), 1)}
        self.paths = {name: self.root / (name.lower() + '.bin') for name in self.images}
        for name, path in self.paths.items(): path.write_bytes(self.images[name])
        patch = mock.patch.object(stenosquid, 'DEFAULT_IMAGES', self.paths)
        patch.start(); self.addCleanup(patch.stop)

    def install(self, keyboard, repair=None):
        with redirect_stdout(io.StringIO()): stenosquid.install_hid(keyboard, repair)

    def test_install_uses_single_base64_pass_and_skips_healthy_base(self):
        keyboard = Keyboard(self.images, verified=['LAPWING'])
        self.install(keyboard)
        self.assertEqual(keyboard.buffers, {name: data for name, data in self.images.items() if name != 'LAPWING'})
        payload = [base64.b64decode(c.split()[-1]) for c in keyboard.commands if c.startswith('base_install data ')]
        self.assertEqual(sum(map(len, payload)), 2400)

    def test_prepare_all_missing_files_before_first_write(self):
        for path in self.paths.values(): path.unlink()
        prepared = []
        keyboard = Keyboard(self.images)
        def prepare(name, expected):
            self.assertFalse(any(' begin ' in c for c in keyboard.commands))
            self.paths[name].write_bytes(self.images[name]); prepared.append(name)
        with mock.patch.object(stenosquid, 'prepare_base', side_effect=prepare): self.install(keyboard)
        self.assertEqual(len(prepared), 3)

    def test_bad_cache_is_not_overwritten(self):
        old = b'JSC4' + b'x' * 1196
        self.paths['PLOVER'].write_bytes(old)
        keyboard = Keyboard({'PLOVER': self.images['PLOVER']})
        def prepare(name, expected, path): path.write_bytes(self.images[name])
        with mock.patch.object(stenosquid, 'prepare_base', side_effect=prepare): self.install(keyboard, 'PLOVER')
        self.assertEqual(self.paths['PLOVER'].read_bytes(), old)

    def test_repair_all_checks_checksums_and_preserves_healthy_bases(self):
        keyboard = Keyboard(self.images, ['LAPWING', 'PLOVER'], ['PLOVER'])
        self.install(keyboard, 'ALL')
        self.assertEqual([c for c in keyboard.commands if ' repair ' in c],
                         ['base_install repair PLOVER', 'base_install repair EXTRAS'])
        self.assertEqual(keyboard.verified, set(self.images))

    def test_repair_one_leaves_other_base_uninstalled(self):
        keyboard = Keyboard(self.images)
        self.install(keyboard, 'PLOVER')
        self.assertEqual(keyboard.verified, {'PLOVER'})

    def test_conflict_gives_scoped_explicit_repair_command(self):
        keyboard = Keyboard(self.images); keyboard.conflict = True
        with self.assertRaisesRegex(RuntimeError, '--repair --base lapwing'): self.install(keyboard)
        self.assertEqual(keyboard.commands[-1], 'base_install abort')
        self.assertFalse(any(' repair ' in c for c in keyboard.commands))

    def test_lost_ack_retries_without_duplicate_data(self):
        keyboard = Keyboard(self.images); keyboard.drop_ack = True
        self.install(keyboard)
        self.assertEqual(keyboard.buffers, self.images)
        self.assertEqual(sum(c.startswith('base_install data 0 ') for c in keyboard.commands), 4)

    def test_bounded_retries_abort_after_three_timeouts(self):
        keyboard = Keyboard(self.images); keyboard.failures = 3
        with self.assertRaises(TimeoutError): self.install(keyboard)
        self.assertEqual(sum(c.startswith('base_install data ') for c in keyboard.commands), 3)
        self.assertEqual(keyboard.commands[-1], 'base_install abort')

    def test_resume_starts_at_last_acknowledged_chunk(self):
        keyboard = Keyboard(self.images); keyboard.resume = 512
        self.install(keyboard)
        self.assertEqual(keyboard.buffers, self.images)
        self.assertFalse(any(c.startswith('base_install data 0 ') for c in keyboard.commands))

    def test_old_protocol_is_rejected(self):
        console = mock.Mock(); console.command.return_value = '1 READY\nLAPWING 4 0 1'
        with self.assertRaisesRegex(RuntimeError, 'protocol mismatch'): stenosquid.hid_install_status(console)

    def test_status_and_verify_are_read_only(self):
        keyboard = Keyboard(self.images, self.images)
        for verify in (False, True):
            with redirect_stdout(io.StringIO()): stenosquid.inspect_hid(keyboard, 'ALL', verify)
        self.assertFalse(keyboard.buffers)

    def test_bad_crc_causes_verify_failure(self):
        keyboard = Keyboard(self.images, self.images, ['PLOVER'])
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit): stenosquid.inspect_hid(keyboard, 'ALL', True)

    def test_cli_install_uses_hid_serial_number_not_serial_port(self):
        keyboard = Keyboard(self.images)
        with mock.patch.object(import_personal, 'open_console', return_value=nullcontext(keyboard)) as connect, \
             mock.patch.object(stenosquid, 'Serial', side_effect=AssertionError('serial opened')), \
             mock.patch.object(sys, 'argv', ['stenosquid.py', '--install', '--serial', 'S1']), redirect_stdout(io.StringIO()):
            stenosquid.main()
        connect.assert_called_once_with('S1')

    def test_backup_and_import_dispatch(self):
        for action, function in (('--backup', 'backup_file'), ('--import', 'import_file')):
            with mock.patch.object(import_personal, function) as call, \
                 mock.patch.object(import_personal, 'open_console', return_value=nullcontext(Keyboard(self.images))), \
                 mock.patch.object(sys, 'argv', ['stenosquid.py', action, 'dict.json', '--serial', 'S1']):
                stenosquid.main()
            call.assert_called_once_with(Path('dict.json'), 'S1')

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

    def test_diag_auto_detects_one_port(self):
        port = object()
        with mock.patch.object(stenosquid, 'find_port', return_value='COM1'), \
             mock.patch.object(stenosquid, 'Serial', return_value=nullcontext(port)), \
             mock.patch.object(stenosquid, 'diag') as diag, \
             mock.patch.object(sys, 'argv', ['stenosquid.py', '--diag']), redirect_stdout(io.StringIO()):
            stenosquid.main()
        self.assertEqual(diag.call_args_list, [mock.call(port, section) for section in ('status', 'log', 'rail', 'bench')])

    def test_serial_provisioning_and_multiple_actions_are_rejected(self):
        for args in (['COM1', '--install'], ['--repair', '--verify'], ['--backup', 'a.json', '--status']):
            with mock.patch.object(sys, 'argv', ['stenosquid.py', *args]), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
                stenosquid.main()
            self.assertEqual(result.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
