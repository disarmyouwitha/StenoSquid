#!/usr/bin/env python3
"""Provision the Native dictionary store over USB HID from one firmware.

--install --confirm-erase explicitly replaces the connected dictionary pool.
The reserved final 64 KiB, pairings, keymap and Practice settings are untouched.
"""

import argparse
import base64
import hashlib
import json
from itertools import chain
from pathlib import Path
import struct
import subprocess
import sys
import time
import zlib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/dictionaries"))
from import_personal import open_console
from build_native_image import build, read_personal, LARGE_POOL_END, POOL_ENDS, SEGMENT, SMALL_POOL_END, SOURCES

UNPROVISIONED = "NATIVE2 UNPROVISIONED 0"
FACTORY_CAPABILITIES = "native_install update factory-member-capabilities"


def check_image(image, manifest):
    from native_update import Snapshot

    pool_end = len(image) // SEGMENT
    if (pool_end not in POOL_ENDS or len(image) % SEGMENT or manifest.get("format") != "SQBCAT02"):
        raise ValueError("Not a native shared-pool image")
    if (
        zlib.crc32(image) != manifest["crc32"]
        or hashlib.sha256(image).hexdigest() != manifest["sha256"]
    ):
        raise ValueError("Native image checksum differs from its manifest")
    absent = None
    factory_names = {identity: name.encode() for identity, name, _, source in SOURCES if source}
    for root in (0, SEGMENT):
        payload_bytes, crc, header_crc, committed = struct.unpack_from("<IIII", image, root + 16)
        payload = image[root + 32:root + 32 + payload_bytes]
        rows, extents = struct.unpack_from("<HH", image, root + 36)
        extent_offset = 32 + rows * 96
        if (image[root:root + 8] != b"SQBCAT02" or rows > 64
                or extents > 253 or payload_bytes != extent_offset + extents * 24
                or committed != 0x434F4D54 or zlib.crc32(image[root:root + 24]) != header_crc
                or zlib.crc32(payload) != crc):
            raise ValueError("Invalid Native image catalog")
        omitted = set()
        factory_extents = set()
        for index in range(rows):
            row = payload[32 + index * 96:32 + (index + 1) * 96]
            identity, enabled, base, member, marker, name_bytes = struct.unpack_from("<IIHBBB", row)
            if not marker:
                if identity in factory_names:
                    if member or base >= extents or base in factory_extents:
                        raise ValueError("Factory dictionaries must have individual flash images.")
                    start, span, size, _, kind, _ = struct.unpack_from("<HHIIIQ", payload, extent_offset + base * 24)
                    if (not 2 <= start < pool_end or not 1 <= span <= pool_end - start or not 8 <= size <= span * SEGMENT
                            or kind != 1 or image[start * SEGMENT:start * SEGMENT + 8] != b"JSC4\x01\x00\x00\x00"):
                        raise ValueError("Invalid individual factory image")
                    factory_extents.add(base)
                continue
            if marker == 1:
                raise ValueError("Experimental snapshot images cannot be provisioned over USB.")
            if (marker != 2 or enabled or base != 65535 or member or not any(row[64:96])
                    or factory_names.get(identity) != row[16:16 + name_bytes]
                    or identity in omitted):
                raise ValueError("Invalid uninstalled factory dictionary")
            omitted.add(identity)
        if absent is not None and absent != omitted:
            raise ValueError("Native catalogs disagree about factory dictionaries")
        absent = omitted
        Snapshot(lambda at, size: image[at:at + size], root + 32, payload_bytes, crc, strict=True, pool_end=pool_end)
    return absent


def command(console, text, timeout=30):
    result = console.command("native_install " + text, timeout=timeout)
    if not result.startswith("OK"):
        raise RuntimeError(result)
    return result


def read_pool_end(console):
    reply = console.command("native_install geometry")
    if reply in ("ERR No native installation active", "ERR Command unavailable on this keyboard"):
        return SMALL_POOL_END
    if reply == f"OK POOL1 {SMALL_POOL_END}":
        return SMALL_POOL_END
    if reply == f"OK POOL1 {LARGE_POOL_END}":
        return LARGE_POOL_END
    raise RuntimeError(f"Cannot determine the external flash geometry. {reply}")


def begin_for_pool(command_text, pool_end):
    return f"large {pool_end} {command_text}" if pool_end == LARGE_POOL_END else command_text


def provision(console, image, manifest, progress=print, *, expected_status=None):
    check_image(image, manifest)
    if console.command(FACTORY_CAPABILITIES) != "OK FACTORY2":
        raise RuntimeError("Update the left firmware to install individual factory dictionaries.")
    status = console.command("native_install status")
    if not status.startswith("NATIVE2 "):
        raise RuntimeError("Flash the matching Native firmware before installing: " + status)
    if expected_status is not None and status != expected_status:
        raise RuntimeError("The keyboard’s dictionary status changed. Reconnect before setting up.")
    pool_end = read_pool_end(console)
    expected_length = pool_end * SEGMENT
    if len(image) > expected_length:
        raise ValueError("The Native image is larger than this keyboard's flash.")
    if len(image) < expected_length:
        image += b"\xff" * (expected_length - len(image))
    command(console, begin_for_pool(f"begin CLEAR {zlib.crc32(image)}", pool_end))
    try:
        write_pool(console, image, progress, write_label="Writing base dictionaries")
    except BaseException:
        try:
            command(console, "abort", timeout=2)
        except (OSError, RuntimeError, TimeoutError):
            pass
        raise


def write_pool(console, image, progress, *, finish=True, write_label="Writing native image"):
    # Write all referenced data before making either catalog visible at boot.
    catalog_end = 2 * SEGMENT
    block_size = 512
    erased = b"\xff" * block_size
    offsets = [
        at for at in chain(range(catalog_end, len(image), block_size), range(0, catalog_end, block_size))
        if image[at : at + block_size] != erased
    ]
    total = len(offsets) * block_size
    pool_end = len(image) // SEGMENT
    for segment in range(pool_end):
        command(console, f"erase {segment}")
        if segment % 16 == 0 or segment + 1 == pool_end:
            progress(f"Erasing shared pool: {segment + 1}/{pool_end}")
    if total:
        progress(f"{write_label}: 0 / {total / 1024:,.1f} KiB (0%)")
    for count, at in enumerate(offsets, 1):
        data = image[at : at + block_size]
        command(console, f"write {at} {base64.b64encode(data).decode()}")
        sent = count * block_size
        if sent == total or sent % (128 * 1024) == 0:
            progress(f"{write_label}: {sent / 1024:,.1f} / {total / 1024:,.1f} KiB ({sent * 100 // total}%)")
    if finish:
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


def prepare_install_image(restore=None, *, packages=None):
    personal = read_personal(restore) if restore else None
    if any(source and not (ROOT / "build" / source).exists() for _, _, _, source in SOURCES):
        subprocess.run([sys.executable, str(ROOT / "tools/dictionaries/prepare.py")], check=True)
    return build(ROOT / "build", personal, packages=packages)


def install(serial=None, image_path=None, restore=None):
    if image_path:
        image = image_path.read_bytes()
        manifest = json.loads(image_path.with_suffix(".json").read_text())
    with open_console(serial) as console:
        serial = console.serial
        if not image_path:
            image, manifest = prepare_install_image(restore)
        provision(console, image, manifest)
    wait_ready(serial)


def wait_ready(serial=None, *, load_state=None, checking=None, opener=None):
    opener = opener or open_console
    time.sleep(3)
    started = time.monotonic()
    deadline = started + 90
    saw_busy = False
    while time.monotonic() < deadline:
        try:
            with opener(serial) as console:
                status = console.command("native_install status", timeout=2)
                if status == "ERR Native flash busy" and not saw_busy:
                    saw_busy = True
                    deadline = started + 180
                    if checking is not None:
                        checking()
                if status.startswith("NATIVE2 READY "):
                    if load_state is not None:
                        return load_state(console)
                    if console.command("list_dictionaries", timeout=2).lstrip().startswith("["):
                        print("Native engine ready. Test KAT, multi-stroke, Practice and browser edits.")
                        return
        except (OSError, RuntimeError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError(
        "The left did not report READY after reboot; image remains available for diagnosis"
    )


if __name__ == "__main__":
    main()
