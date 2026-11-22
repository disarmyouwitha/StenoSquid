#!/usr/bin/env python3
"""Bundle each board's matching firmware pair and standalone Manager together."""
import argparse
from pathlib import Path
import struct
import tempfile
import zipfile


def checked_uf2(path, board):
    data = path.read_bytes()
    if not data or len(data) % 512:
        raise ValueError(f"Invalid UF2 size: {path}")
    lower, upper = (0x1000, 0xd4000) if board == "Boardsource" else (0x26000, 0xec000)
    for offset in range(0, len(data), 512):
        magic, second, flags, address, size, number, count, family = struct.unpack_from("<8I", data, offset)
        if ((magic, second, flags & 0x2000, family) != (0x0a324655, 0x9e5d5157, 0x2000, 0xada52840)
                or not 0 < size <= 476 or not lower <= address < address + size <= upper
                or (offset == 0 and address != lower)
                or number != offset // 512 or count != len(data) // 512
                or struct.unpack_from("<I", data, offset + 508)[0] != 0x0ab16f30):
            raise ValueError(f"UF2 does not match the {board} application partition: {path}")
    return data


def package(firmware, manager_zip, output):
    with zipfile.ZipFile(manager_zip) as manager:
        if manager.namelist() != ["StenoSquidManager.html"] or manager.testzip() is not None:
            raise ValueError("Expected the tested single-HTML Manager ZIP")
        html = manager.read("StenoSquidManager.html")
    # Check every input before publishing either output archive.
    images = {board: {half: checked_uf2(firmware / f"{board}-{half}.uf2", board)
                      for half in ("Left", "Right")} for board in ("Boardsource", "NiceNano")}
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output) as directory:
        for board, name in (("Boardsource", "boardsource.zip"), ("NiceNano", "nicenano.zip")):
            path = Path(directory) / name
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                for half, data in images[board].items():
                    bundle.writestr(f"{half}.uf2", data)
                bundle.writestr("StenoSquidManager.html", html)
            path.replace(output / name)
            print(output / name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--firmware", required=True, type=Path)
    parser.add_argument("--manager-zip", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    package(args.firmware, args.manager_zip, args.output)


if __name__ == "__main__":
    main()
