#!/usr/bin/env python3
"""Build and run the transfer frame/session host tests.

The golden frame is produced here with struct+zlib, independently of the C++
codec, so a shared bug in encoder and decoder cannot hide.
"""
from hostlib import compiler
from pathlib import Path
import struct
import subprocess
import tempfile
import zlib

ROOT = Path(__file__).resolve().parents[2]


def golden_frame():
    body = struct.pack('<BBHIH', 0xB5, 0x02, 0x0102, 0x10, 3) + b'ABC'
    return (body + struct.pack('<I', zlib.crc32(body))).hex()


def main():
    with tempfile.TemporaryDirectory(prefix='steno-transfer-') as folder:
        binary = Path(folder) / 'test_transfer_session'
        subprocess.run([*compiler(), '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror',
                        ROOT / 'tools/tests/test_transfer_session.cc', '-o', binary], check=True)
        subprocess.run([binary, golden_frame()], check=True)


if __name__ == '__main__':
    main()
