#!/usr/bin/env python3
"""Lab tool: flash a lab build onto a left half without touching the board (Windows, macOS, Linux).

The board must already run a lab build made with config/boardsource-wireless-corne/lab-bootloader.conf (every CI "Measure" build
has it). The script sends the USB console command "native_install lab-bootloader", waits for the UF2 bootloader's drive to appear,
copies the file onto it and waits for the keyboard to come back.

    python3 tools/bench/lab_flash.py path/to/firmware.uf2 [--serial USB_SERIAL] [--drive PATH]

The first lab build, and any build without the command, still has to be flashed by hand (double-tap reset). Dictionary flash and
settings are not touched by the reboot. Back up first if the test will write to the keyboard (tools/bench/run_js_upload_hw.py
--backup-to or the Manager's full backup). Never use this on a board you have not agreed to flash.
"""
import argparse
import errno
import os
from pathlib import Path
import shutil
import string
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/dictionaries"))


def uf2_drives():
    """Mounted volumes that look like a UF2 bootloader (they carry INFO_UF2.TXT)."""
    roots = []
    if os.name == "nt":
        roots = [Path(f"{letter}:/") for letter in string.ascii_uppercase]
    elif sys.platform == "darwin":
        roots = list(Path("/Volumes").iterdir())
    else:
        for base in ("/media", "/run/media", "/mnt"):
            base = Path(base)
            if base.is_dir():
                for root in base.iterdir():
                    roots.append(root)
                    try:
                        roots.extend(root.iterdir())
                    except OSError:
                        pass
    found = []
    for root in roots:
        try:
            if (root / "INFO_UF2.TXT").is_file():
                found.append(root)
        except OSError:
            pass
    return found


def wait_for(predicate, seconds, label):
    deadline = time.time() + seconds
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.5)
    raise SystemExit(f"Timed out waiting for {label}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("uf2", type=Path)
    parser.add_argument("--serial", help="USB serial number of the keyboard (needed when two halves are plugged in)")
    parser.add_argument("--drive", type=Path, help="the bootloader drive, if the board is already in the bootloader")
    args = parser.parse_args()
    if not args.uf2.is_file() or args.uf2.suffix.lower() != ".uf2":
        raise SystemExit(f"Not a .uf2 file: {args.uf2}")

    drive = args.drive
    serial = args.serial
    if drive is None:
        existing = uf2_drives()
        if existing:
            raise SystemExit(f"A UF2 drive is already mounted ({existing[0]}); pass --drive {existing[0]} to flash it.")
        from import_personal import open_console

        with open_console(serial) as console:
            serial = console.serial
            reply = console.command("native_install lab-bootloader", timeout=5)
        print(reply)
        if not reply.startswith("OK"):
            raise SystemExit("The keyboard did not accept lab-bootloader: it needs a build with lab-bootloader.conf. "
                             "Flash one by hand (double-tap reset) once.")
        drive = wait_for(lambda: (uf2_drives() or [None])[0], 30, "the UF2 bootloader drive")
    print(f"copying {args.uf2.name} to {drive}")
    copy_error = False
    try:
        shutil.copyfile(args.uf2, drive / args.uf2.name)
    except OSError as error:
        # macOS can report either error when the bootloader ejects during the final write.
        if sys.platform != "darwin" or error.errno not in (errno.EIO, errno.ENXIO) or not serial:
            raise
        copy_error = True
        print("macOS reported an I/O error during the UF2 copy. Waiting for the same keyboard to return.",
              file=sys.stderr)
    time.sleep(2)
    from import_personal import usb_console_devices

    def back():
        try:
            if copy_error and (drive / "INFO_UF2.TXT").is_file():
                return None
            return usb_console_devices(serial)
        except Exception:
            return None

    wait_for(back, 40, "the keyboard to enumerate again")
    if copy_error:
        raise SystemExit("The keyboard is back on USB. Check the installed firmware to confirm the flash.")
    print("flashed; the keyboard is back on USB")


if __name__ == "__main__":
    main()
