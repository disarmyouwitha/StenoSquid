#!/usr/bin/env python3
"""Small USB HID manager for the experimental native-engine build.

Imports merge entries one at a time; a failed entry stops without clearing any
other dictionary. IDs are reported by native status; omit --dictionary for the
current personal dictionary, including after its atomic reset.
"""

import argparse
import base64
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/dictionaries"))
from import_personal import open_console, parse_named
from compile_dict import parse_outline


def checked(console, line):
    result = console.command(line, timeout=60)
    if result != "OK":
        raise RuntimeError(result)


def import_entries(console, identity, entries, names, progress=print):
    checked(console, "native_entry abort")
    for number, (strokes, text) in enumerate(entries.items(), 1):
        outline = names[strokes]
        checked(console, f"native_entry begin {identity} {len(text)} {outline}")
        for offset in range(0, len(text), 512):
            encoded = base64.b64encode(text[offset : offset + 512]).decode()
            checked(console, f"native_entry data {offset} {encoded}")
        deadline = time.monotonic() + 180
        while True:
            response = console.command("native_entry commit", timeout=60)
            if response == "OK":
                break
            if (
                not response.startswith("ERR Native store busy")
                or time.monotonic() >= deadline
            ):
                raise RuntimeError(
                    f"Stopped at {outline} after {number - 1} saved entries: {response}"
                )
            time.sleep(0.05)
        if number % 100 == 0:
            progress(f"Saved {number}/{len(entries)} entries")
    progress(f"Saved {len(entries)} entries in dictionary {identity}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--status", action="store_true")
    modes.add_argument("--import", dest="import_path", type=Path, metavar="USER.JSON")
    modes.add_argument("--remove", metavar="OUTLINE")
    modes.add_argument("--reset-outline", metavar="OUTLINE")
    parser.add_argument(
        "--dictionary",
        type=int,
        help="Stable numeric dictionary ID; defaults to personal",
    )
    parser.add_argument("--serial", help="USB serial number")
    args = parser.parse_args()
    entries, names = (
        parse_named(args.import_path.read_text()) if args.import_path else ({}, {})
    )
    outline = args.remove or args.reset_outline
    if outline:
        parse_outline(outline)
    with open_console(args.serial) as console:
        status = json.loads(console.command("user_dictionary_status"))
        if status.get("format") != "SQBCAT02" or not status.get("ready"):
            raise RuntimeError("A ready Native-Engine firmware is required")
        if args.status:
            print(json.dumps(status, indent=2))
            print(console.command("list_dictionaries"))
            return
        identity = (
            args.dictionary if args.dictionary is not None else status["personal_id"]
        )
        if identity <= 0 or identity > 0xFFFFFFFF:
            parser.error("--dictionary must be a positive 32-bit ID")
        if args.import_path:
            try:
                import_entries(console, identity, entries, names)
            except BaseException:
                try:
                    checked(console, "native_entry abort")
                except (OSError, RuntimeError, TimeoutError):
                    pass
                raise
        else:
            command = "native_remove_entry" if args.remove else "native_reset_entry"
            checked(console, f"{command} {identity} {outline}")


if __name__ == "__main__":
    main()
