#!/usr/bin/env python3
"""Lab tool: compare the JavaScript console + state reader with manager.py on a REAL keyboard (USB, read-only).

Runs manager.load_view(console, ACTION) with Python's own console, then the same through tools/manager/browser/console.js and
state.js (Python relays the HID reports), and prints any difference. Only read commands are sent (status, catalog, flash map,
metadata reads, journal usage); nothing is written to the keyboard.

    python3 tools/bench/run_state_hw.py [refresh|layout_map|enable]
"""
import json
from pathlib import Path
import struct
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
for sub in ("tools/manager", "tools/storage", "tools/dictionaries", "tools/dict_compiler"):
    sys.path.insert(0, str(ROOT / sub))
import manager
from import_personal import open_console

SCRIPT = ROOT / "tools/bench/js/hw_state.mjs"


def read_exact(stream, count):
    data = b""
    while len(data) < count:
        chunk = stream.read(count - len(data))
        if not chunk:
            raise RuntimeError("The JavaScript client exited early.")
        data += chunk
    return data


def run_javascript(console, action):
    process = subprocess.Popen(["node", str(SCRIPT), console.serial, console.transport, action], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, bufsize=0)
    device = console.device
    try:
        while True:
            tag = read_exact(process.stdout, 1)
            if tag == b"W":
                device.write(b"\0" + read_exact(process.stdout, 63))
            elif tag == b"R":
                timeout = struct.unpack("<H", read_exact(process.stdout, 2))[0]
                packet = device.read(63, timeout)
                process.stdin.write(b"R" + bytes(packet).ljust(63, b"\0") if packet else b"N")
            elif tag == b"D":
                return json.loads(read_exact(process.stdout, struct.unpack("<I", read_exact(process.stdout, 4))[0]))
            else:
                raise RuntimeError(f"Unexpected message {tag!r} from the JavaScript client.")
    finally:
        process.stdin.close()
        process.wait(timeout=30)


def diff(path, a, b, out):
    if type(a) is not type(b):
        out.append(f"{path}: {a!r} != {b!r}")
    elif isinstance(a, dict):
        for key in sorted(set(a) | set(b), key=str):
            if key not in a or key not in b:
                out.append(f"{path}.{key}: only in {'python' if key in a else 'javascript'}")
            else:
                diff(f"{path}.{key}", a[key], b[key], out)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append(f"{path}: length {len(a)} != {len(b)}")
        for index, (x, y) in enumerate(zip(a, b)):
            diff(f"{path}[{index}]", x, y, out)
    elif a != b:
        out.append(f"{path}: {a!r} != {b!r}")


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "refresh"
    with open_console() as console:
        print(f"keyboard {console.serial} over {console.transport}; action {action!r}")
        progress, shown = [], []
        started = time.time()
        view = manager.load_view(console, action, progress.append, lambda v: shown.append(json.loads(json.dumps(v))))
        python_ms = round((time.time() - started) * 1000)
        python = json.loads(json.dumps(view))
        started = time.time()
        result = run_javascript(console, action)
        js_ms = round((time.time() - started) * 1000)
    if "error" in result:
        print("JavaScript failed:", result["error"])
        sys.exit(1)
    differences = []
    diff("view", python, result["view"], differences)
    if progress != result["progress"]:
        differences.append(f"progress: {progress} != {result['progress']}")
    diff("shown", shown, result["shown"], differences)
    print(f"python {python_ms} ms, javascript {js_ms} ms (including relay latency)")
    print(f"keys: {sorted(python)}")
    if "usage" in python:
        print(f"usage: {len(python['usage'])} dictionaries, journal records {sum(row['count'] for row in python['usage'].values())}")
    if "layout_map" in python:
        layout = python["layout_map"]
        print(f"layout map: {len(layout['segments'])} segments, {layout['free_segments']} free, {len(layout.get('images') or [])} images")
    if differences:
        print(f"{len(differences)} DIFFERENCES:")
        for line in differences[:40]:
            print("  ", line)
        sys.exit(2)
    print("IDENTICAL: the JavaScript result matches manager.load_view on the real keyboard")


if __name__ == "__main__":
    main()
