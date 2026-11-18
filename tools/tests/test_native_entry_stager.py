#!/usr/bin/env python3
"""Run the fixed-memory firmware checkpoint coordinator against strict NOR."""

from pathlib import Path
import subprocess
import tempfile
from hostlib import compiler

ROOT = Path(__file__).resolve().parents[2]


def main():
    with tempfile.TemporaryDirectory() as directory:
        binary = Path(directory) / "native-entry-stager"
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
                str(ROOT / "tools/tests/test_native_entry_stager.cc"),
                "-o",
                str(binary),
            ],
            check=True,
        )
        subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    main()
