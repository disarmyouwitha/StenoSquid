#!/usr/bin/env python3
"""Verify the original compact compiler footprint and checksums."""
from pathlib import Path
import sys
import zlib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools/dictionaries'))
from manifest import BASES


def main():
    expected = {'LAPWING': (1830444, 0x00f2d636), 'PLOVER': (2339620, 0x3e8f8d88),
                'EXTRAS': (469812, 0x98af1ecc)}
    for name, base in BASES.items():
        blob = (ROOT / 'build' / base['directory'] / base['image']).read_bytes()
        assert (len(blob), zlib.crc32(blob)) == expected[name], f'{name}: compiled image changed'
    print('PASS: pinned compact image sizes and CRCs')


if __name__ == '__main__':
    main()
