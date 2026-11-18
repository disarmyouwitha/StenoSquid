#!/usr/bin/env python3
"""Exercise the native fixed-workspace reader against 200,000 packed entries."""

from pathlib import Path
import struct
import subprocess
import sys
import tempfile

from hostlib import compiler

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/storage"))
from packed_index import build, fingerprint


def main():
    assert fingerprint(b"") == 14695981039346656037
    assert fingerprint(b"a") == 0xAF63DC4C8601EC8C
    try:
        build([(3, i) for i in range(17)])
    except ValueError:
        pass
    else:
        raise AssertionError("Unbounded collision group accepted")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        # Deliberate hash collisions; all 200k distinct full keys must resolve.
        data = build((i // 4, i * 16) for i in range(200000))
        (path / "index.bin").write_bytes(data)
        (path / "records.bin").write_bytes(
            b"".join(
                struct.pack("<IBBHI4s", 1, 0, 1, 4, i + 1, b"word")
                for i in range(200000)
            )
        )
        binary = path / "test"
        subprocess.run(
            [
                *compiler(),
                "-std=c++17",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-I",
                str(ROOT / "tools/storage"),
                str(ROOT / "tools/tests/test_packed_index.cc"),
                "-o",
                str(binary),
            ],
            check=True,
        )
        subprocess.run(
            [str(binary), str(path / "index.bin"), str(path / "records.bin")],
            check=True,
        )


if __name__ == "__main__":
    main()
