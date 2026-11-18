#!/usr/bin/env python3
"""Cross-check the bounded native journal with the independent Python codec."""

from pathlib import Path
import subprocess
import sys
import tempfile
from hostlib import compiler

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/storage"))
from binary_journal import BinaryJournal
from hybrid import FakeNor, SEGMENT, canonical


def main():
    nor = FakeNor(3)
    model = object.__new__(BinaryJournal)
    model.nor = nor
    extent = dict(start=2, count=1, epoch=1)
    model._initialize(extent)
    entries = [
        dict(id=1, outline="KAT", op="set", text="cat"),
        dict(id=1, outline="HE/HROE", op="set", text="ça va"),
        dict(id=2, outline="#^+STKPWHRAO*EUFRPBLGTSDZ", op="remove", text=None),
        dict(id=2, outline="KAT", op="reset", text=None),
    ]
    offset = 256
    for sequence, entry in enumerate(entries, 1):
        offset += model._write(extent, offset, entry, sequence)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        (path / "input.bin").write_bytes(nor.data)
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
                str(ROOT / "tools/tests/test_native_journal.cc"),
                "-o",
                str(binary),
            ],
            check=True,
        )
        subprocess.run(
            [str(binary), str(path / "input.bin"), str(path / "output.bin")], check=True
        )
        model.nor = FakeNor(data=(path / "output.bin").read_bytes())
        decoded, end, sealed = model._scan(extent)
        assert not sealed and end > offset and len(decoded) == 5
        assert decoded[-1]["text"] == "ça va" and decoded[-1]["sequence"] == 5
        assert canonical(decoded[-1]["outline"]) == canonical("K/A")
        assert model.nor.data[: 2 * SEGMENT] == nor.data[: 2 * SEGMENT]


if __name__ == "__main__":
    main()
