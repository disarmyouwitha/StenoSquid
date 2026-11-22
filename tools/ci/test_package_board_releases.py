"""Verify board downloads contain the matching pair and identical Manager HTML."""
from pathlib import Path
import struct
import tempfile
import unittest
import zipfile

from package_board_releases import package


def image(address, payload):
    data = bytearray(512)
    struct.pack_into("<8I", data, 0, 0x0a324655, 0x9e5d5157, 0x2000, address, 256, 0, 1, 0xada52840)
    data[32:288] = payload * 256
    struct.pack_into("<I", data, 508, 0x0ab16f30)
    return bytes(data)


class BoardReleaseTest(unittest.TestCase):
    def test_each_board_contains_its_pair_and_the_manager(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            html = b'<!doctype html><title>StenoSquid Manager</title>'
            with zipfile.ZipFile(root / 'manager.zip', 'w') as bundle:
                bundle.writestr('StenoSquidManager.html', html)
            expected = {}
            for board, address in [('Boardsource', 0x1000), ('NiceNano', 0x26000)]:
                expected[board] = {}
                for half, fill in [('Left', b'L'), ('Right', b'R')]:
                    data = image(address, fill)
                    (root / f'{board}-{half}.uf2').write_bytes(data)
                    expected[board][f'{half}.uf2'] = data
            package(root, root / 'manager.zip', root / 'out')
            self.assertEqual({path.name for path in (root / 'out').iterdir()}, {'boardsource.zip', 'nicenano.zip'})
            for board, name in [('Boardsource', 'boardsource.zip'), ('NiceNano', 'nicenano.zip')]:
                with zipfile.ZipFile(root / 'out' / name) as bundle:
                    self.assertIsNone(bundle.testzip())
                    self.assertEqual(set(bundle.namelist()), {'Left.uf2', 'Right.uf2', 'StenoSquidManager.html'})
                    self.assertEqual(bundle.read('StenoSquidManager.html'), html)
                    for member, data in expected[board].items():
                        self.assertEqual(bundle.read(member), data)
            # Refuse a mismatched board image before writing either archive.
            (root / 'Boardsource-Left.uf2').write_bytes(image(0x26000, b'X'))
            with self.assertRaisesRegex(ValueError, 'Boardsource'):
                package(root, root / 'manager.zip', root / 'invalid')
            self.assertFalse((root / 'invalid').exists())


if __name__ == '__main__':
    unittest.main()
