#!/usr/bin/env python3
"""Provision the experimental native engine over USB HID from one firmware.

--install --confirm-erase explicitly replaces all 255 shared-pool segments.
The reserved final 64 KiB, pairings, keymap and Practice settings are untouched.
"""

import argparse
import base64
import hashlib
import json
from itertools import chain
from pathlib import Path
import subprocess
import sys
import time
import zlib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/dictionaries"))
from import_personal import open_console
from build_native_image import build, read_personal, LIMIT, SEGMENT, SOURCES


def check_image(image, manifest):
    if len(image) != LIMIT or manifest.get("format") != "SQBCAT02":
        raise ValueError("Not a native shared-pool image")
    if (
        zlib.crc32(image) != manifest["crc32"]
        or hashlib.sha256(image).hexdigest() != manifest["sha256"]
    ):
        raise ValueError("Native image checksum differs from its manifest")


def command(console, text, timeout=30):
    result = console.command("native_install " + text, timeout=timeout)
    if not result.startswith("OK"):
        raise RuntimeError(result)
    return result


def provision(console, image, manifest, progress=print):
    check_image(image, manifest)
    status = console.command("native_install status")
    versions = ("NATIVE1 ", "NATIVE2 ") if manifest.get("schema") == 1 else ("NATIVE2 ",)
    if not status.startswith(versions):
        raise RuntimeError("Flash the matching Native firmware before installing: " + status)
    command(console, f"begin CLEAR {manifest['crc32']}")
    try:
        write_pool(console, image, progress)
    except BaseException:
        try:
            command(console, "abort", timeout=2)
        except (OSError, RuntimeError, TimeoutError):
            pass
        raise


def write_pool(console, image, progress):
    for segment in range(255):
        command(console, f"erase {segment}")
        if segment % 16 == 0:
            progress(f"Erasing shared pool: {segment + 1}/255")
    sent = 0
    # Write all referenced data before making either catalog visible at boot.
    catalog_end = 2 * SEGMENT
    offsets = chain(range(catalog_end, len(image), 512), range(0, catalog_end, 512))
    for at in offsets:
        data = image[at : at + 512]
        if data == b"\xff" * len(data):
            continue
        command(console, f"write {at} {base64.b64encode(data).decode()}")
        sent += len(data)
        if sent % (128 * 1024) == 0:
            progress(f"Writing native image: {sent // 1024} KiB")
    progress("Verifying the complete shared pool; the left will reboot afterward")
    command(console, "finish", timeout=180)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--install", action="store_true")
    mode.add_argument("--status", action="store_true")
    parser.add_argument("--confirm-erase", action="store_true")
    parser.add_argument("--serial", help="USB serial number, not a serial port")
    parser.add_argument("--image", type=Path)
    parser.add_argument("--restore", type=Path, metavar="BACKUP.JSON")
    args = parser.parse_args()
    if args.status:
        with open_console(args.serial) as console:
            print(console.command("native_install status"))
        return
    if not args.confirm_erase:
        parser.error(
            "A clean provision needs --confirm-erase; it replaces all external dictionaries"
        )
    if args.image and args.restore:
        parser.error("--restore builds an image and cannot be combined with --image")
    install(args.serial, args.image, args.restore)


def install(serial=None, image_path=None, restore=None):
    if image_path:
        image = image_path.read_bytes()
        manifest = json.loads(image_path.with_suffix(".json").read_text())
    else:
        personal = read_personal(restore) if restore else None
        if any(
            source and not (ROOT / "build" / source).exists()
            for _, _, _, source in SOURCES
        ):
            subprocess.run(
                [sys.executable, str(ROOT / "tools/dictionaries/prepare.py")],
                check=True,
            )
        image, manifest = build(ROOT / "build", personal)
    with open_console(serial) as console:
        provision(console, image, manifest)
    wait_ready(serial)


def wait_ready(serial=None):
    time.sleep(3)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            with open_console(serial) as console:
                status = console.command("native_install status", timeout=2)
                if status.startswith(("NATIVE1 READY ", "NATIVE2 READY ")):
                    print(
                        "Native engine ready. Test KAT, multi-stroke, Practice and browser edits."
                    )
                    return
        except (OSError, RuntimeError, TimeoutError):
            pass
        time.sleep(1)
    raise RuntimeError(
        "The left did not report READY after reboot; image remains available for diagnosis"
    )


if __name__ == "__main__":
    main()
