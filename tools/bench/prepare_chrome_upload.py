#!/usr/bin/env python3
"""Lab tool: prepare an in-browser upload test for tools/bench/webhid_upload.html.

The Manager's update planner is still Python, so Python prepares the plan and the BROWSER runs the transfer:
  1. (optional) uninstall the factory dictionary over USB so there is something to install,
  2. plan the install read-only (begin, snapshot, plan, abort) and write the plan as JSON.
The page then opens its own staged-update session (the `begin` command stored in the JSON), uploads the
same plan with the JavaScript client over WebHID, and posts its stage timings back.

    python3 tools/bench/prepare_chrome_upload.py lapwing-base OUT.json [--uninstall-first]
"""
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
for sub in ("tools/storage", "tools/dictionaries", "tools/dict_compiler"):
    sys.path.insert(0, str(ROOT / sub))
import native_base_update as base
from build_native_image import FACTORY_KEYS
from import_personal import open_console
from native_update import abort, read_snapshot, update_command


def open_when_ready(limit=240):
    begun = time.time()
    while True:
        manager = open_console()
        try:
            return manager, manager.__enter__()
        except RuntimeError:
            if time.time() - begun > limit:
                raise
            time.sleep(0.5)


def main():
    package, out = sys.argv[1], Path(sys.argv[2])
    uninstall_first = "--uninstall-first" in sys.argv
    identity, name, _, _ = FACTORY_KEYS[package]
    manager, console = open_when_ready()
    try:
        if uninstall_first:
            report = base.factory_package(console, package, restore=False, progress=lambda _: None)
            base.factory_package(console, package, restore=False, confirmation=report["confirmation"], progress=lambda _: None)
            print(f"uninstalled {name}; waiting for the keyboard to restart", flush=True)
            manager.__exit__(None, None, None)
            time.sleep(4)
            manager, console = open_when_ready()
        begin = f"begin factory-member {identity}"
        answer = update_command(console, begin)
        try:
            snapshot, protected = read_snapshot(console, answer, metadata_only=True)
            update, report = base.plan_package(snapshot, protected, package, True, base.prepare(name))
        finally:
            abort(console)
    finally:
        manager.__exit__(None, None, None)
    out.write_text(json.dumps({
        "name": name, "beginCommand": "native_install update " + begin,
        "segments": [[segment, block.hex()] for segment, block in update.segments.items()],
        "metadata": update.metadata, "payload": update.payload.hex(),
        "plan": {key: report[key] for key in ("package_segments", "staging_segments", "free_now", "largest_run_now")},
    }))
    staged = sum(len(block) for block in update.segments.values())
    print(f"plan for {name}: {len(update.segments)} segments, {staged} staged bytes -> {out}")


if __name__ == "__main__":
    main()
