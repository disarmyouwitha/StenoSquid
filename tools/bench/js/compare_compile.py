#!/usr/bin/env python3
"""Prove the JavaScript compiler spike against the Python compiler on the real pinned dictionaries.

    python3 tools/bench/js/compare_compile.py [REPO_ROOT]     (needs node >= 20 on PATH)

For each input the Python compiler writes the reference image, then compile_dict.mjs compiles the same JSON
and the images are compared BYTE FOR BYTE. Timings (ms) are printed for both sides. Equality is the
requirement; speed is only meaningful when the bytes match.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

root = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[3]).resolve()
for sub in ("tools/storage", "tools/dictionaries", "tools/dict_compiler"):
    sys.path.insert(0, str(root / sub))
from compile_dict import DictionaryCollectionBuilder  # noqa: E402

XIP = 0x12000000
CASES = [
    ("lapwing-base", [("lapwing", "build/lapwing/lapwing-base.json")], 0),
    ("plover-main", [("plover", "build/plover/plover-main.json")], 0),
    ("lapwing-base@xip", [("lapwing", "build/lapwing/lapwing-base.json")], XIP + 0x20000),
    ("extras-proper-nouns", [("proper", "build/extras/lapwing-proper-nouns.json")], 0),
    ("two-dictionary-collection", [("lapwing", "build/lapwing/lapwing-base.json"), ("uk", "build/extras/lapwing-uk-additions.json")], 0),
]

with tempfile.TemporaryDirectory(prefix="steno-jscompare-") as folder:
    folder = Path(folder)
    manifest = []
    for name, inputs, base in CASES:
        entries = [(label, json.loads((root / path).read_bytes())) for label, path in inputs]
        start = time.perf_counter()
        image = DictionaryCollectionBuilder(base).build(entries)
        python_ms = (time.perf_counter() - start) * 1000
        (folder / f"{name}.ref").write_bytes(image)
        manifest.append({"name": name, "base": base, "python_ms": round(python_ms, 1),
                         "inputs": [{"label": label, "path": str(root / path)} for label, path in inputs]})
    (folder / "cases.json").write_text(json.dumps(manifest))
    result = subprocess.run(["node", str(Path(__file__).with_name("compare_compile.mjs")), str(folder)],
                            capture_output=True, text=True)
    sys.stderr.write(result.stderr)
    if result.returncode:
        raise SystemExit(result.returncode)
    rows = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    print(f"{'case':<28}{'bytes':>10}{'identical':>11}{'python ms':>11}{'naive js':>10}{'typed js':>10}{'warm js':>9}{'speedup':>9}")
    ok = True
    for row, case in zip(rows, manifest):
        ok &= row["identical"]
        print(f"{row['name']:<28}{row['bytes']:>10}{str(row['identical']):>11}{case['python_ms']:>11.0f}"
              f"{row['naive_js_ms']:>10.0f}{row['js_ms']:>10.0f}{row['warm_js_ms']:>9.0f}{case['python_ms'] / row['js_ms']:>8.1f}x")
    raise SystemExit(0 if ok and len(rows) == len(CASES) else 1)
