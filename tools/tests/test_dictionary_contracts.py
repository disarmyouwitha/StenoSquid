#!/usr/bin/env python3
"""Tie prepared image CRCs and host layout constants to the firmware headers."""
from pathlib import Path
import re
import sys
import zlib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools/dictionaries'))
from manifest import BASES, PERSONAL_START, PERSONAL_SEGMENTS, SEGMENT_BYTES


def main():
    header = (ROOT / 'src/steno_squid/lapwing_flash.h').read_text(encoding='utf-8')
    store = (ROOT / 'src/steno_squid/user_dictionary_log.h').read_text(encoding='utf-8')
    expected = {'LAPWING': (1830444, 0x00f2d636), 'PLOVER': (2339620, 0x3e8f8d88),
                'EXTRAS': (469812, 0x98af1ecc)}
    for name, base in BASES.items():
        blob = (ROOT / 'build' / base['directory'] / base['image']).read_bytes()
        assert (len(blob), zlib.crc32(blob)) == expected[name], f'{name}: compiled image changed'
        if name != 'LAPWING':
            value = re.search(rf'#define {name}_FLASH_OFFSET (0x[0-9a-fA-F]+)', header)[1]
            assert int(value, 16) == base['offset']
    assert int(re.search(r'REGION_START = (0x[0-9a-fA-F]+)', store)[1], 16) == PERSONAL_START
    assert int(re.search(r'SEG_COUNT = (\d+)', store)[1]) == PERSONAL_SEGMENTS
    assert int(re.search(r'SEG_BYTES = (0x[0-9a-fA-F]+)', store)[1], 16) == SEGMENT_BYTES
    print('PASS: pinned image sizes/CRCs and host/firmware flash layout agree')


if __name__ == '__main__':
    main()
