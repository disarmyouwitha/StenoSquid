"""Time the Manager's heaviest real workloads. Pure standard library, so the same file runs under
CPython and under Pyodide (see manager_workloads.mjs), which is the point: it separates
'Python is slow' from 'Pyodide is slow' from 'the bridge is slow'.

    python3 tools/bench/manager_workloads.py [REPO_ROOT]

Inputs are the pinned dictionaries from tools/dictionaries/prepare.py (build/lapwing, build/plover).
Prints one JSON object of milliseconds, so runs are comparable and scriptable.
"""
import base64
import hashlib
import json
from pathlib import Path
import sys
import time
import zlib


def run(root):
    root = Path(root)
    for sub in ("tools/storage", "tools/dictionaries", "tools/dict_compiler", "tools/manager"):
        sys.path.insert(0, str(root / sub))
    from compile_dict import DictionaryCollectionBuilder
    from hybrid import compact_entries
    from verify_mapped_dict import verify

    out = {}

    def timed(name, function):
        start = time.perf_counter()
        try:
            value = function()
        except Exception as error:  # Record, do not hide: a failing workload is a finding.
            out[name + "_error"] = f"{type(error).__name__}: {error}"
            return None
        out[name + "_ms"] = round((time.perf_counter() - start) * 1000, 1)
        return value

    for label, relative in (("lapwing", "build/lapwing/lapwing-base.json"), ("plover", "build/plover/plover-main.json")):
        raw = (root / relative).read_bytes()
        source = timed(f"{label}_json_parse", lambda: json.loads(raw))
        out[f"{label}_entries"] = len(source)
        out[f"{label}_json_bytes"] = len(raw)
        timed(f"{label}_sha256", lambda: hashlib.sha256(raw).digest())
        image = timed(f"{label}_compile", lambda: DictionaryCollectionBuilder(0).build([(label, source)]))
        out[f"{label}_image_bytes"] = len(image)
        timed(f"{label}_verify_every_entry", lambda: verify(source, image, 0))
        count = int.from_bytes(image[4:6], "little")
        decoded = timed(f"{label}_decode_all_entries", lambda: len(list(compact_entries(bytes(image), 0, 0, count))))
        out[f"{label}_decoded"] = decoded
        timed(f"{label}_crc32", lambda: zlib.crc32(image))

    blob = bytes(range(256)) * (4 * 1024 * 1024 // 256)  # 4 MiB, the size class of a large update
    timed("crc32_4MiB", lambda: zlib.crc32(blob))
    encoded = timed("base64_encode_4MiB", lambda: base64.b64encode(blob))
    timed("base64_decode_4MiB", lambda: base64.b64decode(encoded, validate=True))
    return out


if __name__ == "__main__":
    print(json.dumps(run(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[2])))
