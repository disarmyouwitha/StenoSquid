#!/usr/bin/env python3
"""Exercise the firmware hint reader against every one-stroke factory entry.

Run after preparing the pinned dictionary sources. Requires a C++17 compiler.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from hostlib import compiler as host_compiler

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/storage"))
from build_native_image import SOURCES, XIP, build
from compile_dict import parse_outline


def main():
    sources = ROOT / "build"
    pool, manifest = build(sources)
    files = {identity: source for identity, _, _, source in SOURCES if source}
    with tempfile.TemporaryDirectory() as directory:
        executable = Path(directory) / "test-hint-index"
        subprocess.run([*host_compiler("c++"), "-std=c++17", "-Wall", "-Wextra", "-Werror",
                        str(Path(__file__).with_suffix(".cc")), "-o", str(executable)], check=True)

        for image in manifest["base_images"]:
            member, = image["dictionaries"]
            source = sources / files.pop(member["id"])
            best = {}
            for outline, text in json.loads(source.read_text(encoding="utf-8")).items():
                strokes = parse_outline(outline)
                if len(strokes) != 1 or not strokes[0]:
                    continue
                mask = strokes[0]
                old = best.get(text)
                if old is None or (mask.bit_count(), mask) < (old.bit_count(), old):
                    best[text] = mask
            # The line protocol skips control characters; firmware handles
            # them as normal bytes in its bounded search.
            queries = [f"{mask}\t{text}" for text, mask in best.items() if "\n" not in text and "\r" not in text]
            queries += ["0\t__missing_hint__", "0\t", "--NEXT--"]
            start = image["address"]
            binary = Path(directory) / "dictionary.bin"
            binary.write_bytes(pool[start:start + image["bytes"]])
            subprocess.run([str(executable), str(binary), hex(XIP + start)],
                           input="\n".join(queries) + "\n", text=True, check=True)
    assert not files, "One or more factory dictionaries were missing from the installed pool"


if __name__ == "__main__":
    main()
