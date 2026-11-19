#!/usr/bin/env python3
"""Check read-only root classification and the map wire format."""
from pathlib import Path
import subprocess
import sys
import tempfile

from hostlib import compiler

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/storage"))
from native_layout import parse_layout, read_layout


def layer(assignments=(), background="."):
    data = list("cc" + background * 253 + "x")
    for index, value in assignments:
        data[index] = value
    return "".join(data)


def main():
    old = layer([(2, "j"), (3, "h"), (4, "b"), (9, "f"), (255, "x")])
    new = layer([(3, "j"), (4, "b"), (6, "s"), (7, "s"), (8, "h"), (10, "f")])
    reply = f"OK MAP1 1 w vv {old} {new}"

    class Console:
        calls = []

        def command(self, command, timeout=30):
            self.calls.append(command)
            return reply

    console = Console()
    report = read_layout(console)
    assert console.calls == ["native_install update layout-map"]
    assert report["free_segments"] == 245 and report["largest_free_run"] == 244
    assert report["segments"][2]["kind"] == "retained-journal"
    assert report["segments"][3]["shared"] and report["segments"][4]["shared"]
    assert report["segments"][6]["kind"] == "s"
    assert report["segments"][9]["kind"] == "retained"
    assert report["segments"][0]["kind"] == "c" and report["segments"][255]["kind"] == "x"
    pinned = parse_layout(reply.replace("1 w vv", "1 p vv"))
    assert pinned["staging_blocked"] and pinned["free_segments"] == 245
    suspect = parse_layout(f"OK MAP1 1 r ?v {layer(background='?')} {new}")
    assert suspect["free_segments"] is None and suspect["segments"][50]["kind"] == "?"
    for bad in (reply[:-1], reply.replace("1 w", "2 w"), reply.replace(" vv ", " ev ")):
        try:
            parse_layout(bad)
        except ValueError:
            pass
        else:
            raise AssertionError("Accepted invalid layout reply")
    with tempfile.TemporaryDirectory() as folder:
        binary = Path(folder) / "layout"
        subprocess.run([*compiler(), "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-I", str(ROOT / "tools/storage"), str(ROOT / "tools/tests/test_native_layout.cc"),
                        "-o", str(binary)], check=True)
        subprocess.run([str(binary)], check=True)
    print("Flash map: 256 segments, retained-only journals, shared extents, unknowns, bounded read-only metadata")


if __name__ == "__main__":
    main()
