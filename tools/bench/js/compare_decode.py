#!/usr/bin/env python3
"""Prove the JavaScript decoder against Python's compact_entries on real images, and time both.

    python3 tools/bench/js/compare_decode.py [REPO_ROOT]
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
from hybrid import compact_entries  # noqa: E402

CASES = [("lapwing-base", "build/lapwing/lapwing-base.json"), ("plover-main", "build/plover/plover-main.json")]
with tempfile.TemporaryDirectory(prefix="steno-jsdecode-") as folder:
    folder = Path(folder)
    manifest = []
    for name, path in CASES:
        image = DictionaryCollectionBuilder(0).build([(name, json.loads((root / path).read_bytes()))])
        start = time.perf_counter()
        entries = list(compact_entries(image, 0, 0, 1))
        python_ms = (time.perf_counter() - start) * 1000
        (folder / f"{name}.bin").write_bytes(image)
        (folder / f"{name}.json").write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        manifest.append({"name": name, "python_ms": round(python_ms, 1), "count": len(entries)})
    (folder / "cases.json").write_text(json.dumps(manifest))
    script = Path(__file__).with_name("compare_decode.mjs")
    result = subprocess.run(["node", str(script), str(folder)], capture_output=True, text=True)
    sys.stderr.write(result.stderr)
    rows = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    print(f"{'case':<16}{'entries':>9}{'identical':>11}{'python ms':>11}{'js ms':>8}{'speedup':>9}")
    ok = len(rows) == len(CASES)
    for row, case in zip(rows, manifest):
        ok &= row["identical"]
        print(f"{row['name']:<16}{case['count']:>9}{str(row['identical']):>11}{case['python_ms']:>11.0f}{row['js_ms']:>8.0f}"
              f"{case['python_ms'] / row['js_ms']:>8.1f}x")
    raise SystemExit(0 if ok else 1)
