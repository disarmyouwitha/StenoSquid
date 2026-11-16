"""Compile real JSON for the native firmware snapshot reader and fault tests."""

from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import zlib

from import_personal import compile_body, parse_dictionary


def main():
    rows = parse_dictionary('{"K":"new","K/P":"ça va","-S":"{^s}"}')
    # Cross page and sector boundaries with a legal but large dictionary.
    rows.update({(stroke, 8, 16): b"extra text" for stroke in range(1000, 1150)})
    body, maximum = compile_body(rows, 4 * 1024 * 1024)
    repo = Path(__file__).resolve().parents[2]
    compiler = shutil.which("clang++") or shutil.which("g++")
    with tempfile.TemporaryDirectory() as folder:
        file = Path(folder) / "body.bin"
        file.write_bytes(struct.pack("<IIII", len(rows), len(body), maximum, zlib.crc32(body)) + body)
        executable = Path(folder) / "test_snapshot"
        subprocess.run([compiler, "-std=c++17", "-O1", "-g", "-fsanitize=undefined",
                        "-Wall", "-Wextra", "-Werror",
                        str(repo / "tools/tests/test_user_dictionary_snapshot.cc"),
                        "-o", str(executable)], check=True)
        subprocess.run([str(executable), str(file)], check=True)


if __name__ == "__main__":
    main()
