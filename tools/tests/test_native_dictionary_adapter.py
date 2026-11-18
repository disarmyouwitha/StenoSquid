#!/usr/bin/env python3
"""Test adapter ownership/semantics; firmware CI checks the pinned Javelin ABI."""

from pathlib import Path
import subprocess
import tempfile
from hostlib import compiler

ROOT = Path(__file__).resolve().parents[2]


def main():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        (path / "dictionary").mkdir()
        (path / "dictionary/dictionary.h").write_text(
            (ROOT / "tools/tests/native_dictionary_stub.h").read_text()
        )
        binary = path / "adapter"
        subprocess.run(
            [
                *compiler(),
                "-std=c++17",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-I",
                str(path),
                "-I",
                str(ROOT / "src/steno_squid"),
                str(ROOT / "tools/tests/test_native_dictionary_adapter.cc"),
                "-o",
                str(binary),
            ],
            check=True,
        )
        subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    main()
