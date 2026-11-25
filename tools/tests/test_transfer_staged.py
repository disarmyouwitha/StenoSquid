#!/usr/bin/env python3
"""Run the transfer session against the real NativeStage using the planner's real plans."""
from hostlib import compiler
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/tests"))
sys.path.insert(0, str(ROOT / "tools/storage"))
sys.path.insert(0, str(ROOT / "tools/dictionaries"))
from test_native_stage import fixture, plan, write_plan


def main():
    with tempfile.TemporaryDirectory(prefix="steno-transfer-staged-") as directory:
        path = Path(directory)
        image, snapshot, protected = fixture(path)
        (path / "before.bin").write_bytes(image)
        write_plan(path / "import.bin", plan(snapshot, protected, {(1,): b"changed", (7, 8): "ça".encode()}))
        write_plan(path / "compact.bin", plan(snapshot, protected))
        binary = path / "test"
        subprocess.run([*compiler(), "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-I", str(ROOT / "tools/storage"), ROOT / "tools/tests/test_transfer_staged.cc",
                        "-o", binary], check=True)
        subprocess.run([binary, path / "before.bin", path / "import.bin", path / "compact.bin"], check=True)


if __name__ == "__main__":
    main()
