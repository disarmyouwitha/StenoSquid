#!/usr/bin/env python3
"""Real compiled SQS1 search tables and bounded query probes."""
from pathlib import Path
import struct
import subprocess
import tempfile

from hostlib import compiler
from test_native_stage import ROOT
from compact_search_index import validate_search_index
from native_snapshot import compile_snapshot


def main():
    with tempfile.TemporaryDirectory() as folder:
        directory = Path(folder)
        binary = directory / "search"
        subprocess.run([*compiler(), "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-I", str(ROOT / "src/steno_squid"), str(ROOT / "tools/tests/test_compact_search.cc"),
                        "-o", str(binary)], check=True)
        small = {(524552,): b"cat", (4, 524552): b"cat", (8,): b"cat",
                 (524552, 524552, 8): b"catalog", (16,): "ça va".encode()}
        large = {(i // 1024 + 1, i % 1024 + 1): f"translation {i % 3000}".encode()
                 for i in range(200000)}
        for name, entries in (("small", small), ("large", large)):
            compiled = compile_snapshot("Indexed", entries)
            assert validate_search_index(compiled.data)
            assert validate_search_index(compiled.at(0x12000000), 0x12000000)
            native = directory / f"{name}.bin"
            native.write_bytes(compiled.data)
            subprocess.run([str(binary), str(native), name], check=True)
        legacy = compile_snapshot("Legacy", small, search=False)
        assert not validate_search_index(legacy.data)
        corrupt = bytearray(compile_snapshot("Indexed", small).data)
        legacy_bytes = struct.unpack_from("<I", corrupt, len(corrupt) - 16)[0]
        corrupt[legacy_bytes] ^= 1
        try:
            validate_search_index(corrupt)
        except ValueError:
            pass
        else:
            raise AssertionError("Corrupt compact search token accepted")


if __name__ == "__main__":
    main()
