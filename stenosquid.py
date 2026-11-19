"""Install the base dictionaries, or import or back up a personal Plover JSON file over USB.

Use --install to provision the firmware's dictionaries. On the fixed layout,
--base selects an image for status, repair or verification. Native provisioning
requires explicit permission to erase the pool and can seed a personal backup.
"""

import argparse
import base64
import json
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import zlib


def Serial(*args, **kwargs):
    try:
        from serial import Serial as open_port
    except ImportError:
        raise SystemExit("Install the USB serial dependency with: python3 -m pip install pyserial") from None
    return open_port(*args, **kwargs)


CHUNK_BYTES = 512
REPO = Path(__file__).resolve().parent
DICTIONARY_TOOLS = REPO / "tools" / "dictionaries"
sys.path.insert(0, str(DICTIONARY_TOOLS))
# Where the prepare scripts and the Lapwing build instructions write each image.
DEFAULT_IMAGES = {
    "LAPWING": REPO / "build" / "lapwing" / "lapwing-base.bin",
    "PLOVER": REPO / "build" / "plover" / "plover-main.bin",
    "EXTRAS": REPO / "build" / "extras" / "lapwing-extras.bin",
}
def read_image(path):
    """A JSC4 image file, or an error that says how to build it."""
    if not path.is_file():
        raise SystemExit(f"{path} not found. Build it with the matching prepare script first.")
    image = path.read_bytes()
    if image[:4] != b"JSC4" or len(image) % 4:
        raise SystemExit(f"{path} is not a word-aligned JSC4 image")
    return image


def check_matches_firmware(image, name, size, crc, path):
    if len(image) != size or zlib.crc32(image) != crc:
        raise SystemExit(
            f"{path} is not the {name} image this firmware was built for. It expects "
            f"{size:,} bytes with CRC32 {crc:08x}; the file has {len(image):,} bytes with "
            f"CRC32 {zlib.crc32(image):08x}. Rebuild the image, or flash the firmware that "
            "was built with it.")


def prepare_base(name, expected=None, output_path=None):
    path = output_path or DEFAULT_IMAGES[name]
    from prepare import prepare
    prepared = prepare(name, path.parent)
    if prepared != path:
        prepared.replace(path)


def hid_install_status(console):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        reply = console.command("base_install status", timeout=120)
        header, *rows = reply.splitlines()
        if header == "2 LOADING":
            time.sleep(0.25)
            continue
        if header == "2 ERROR":
            raise RuntimeError("The keyboard could not bring up dictionary flash. Check its connection and power.")
        if header not in ("2 INSTALL", "2 READY") or not rows:
            raise RuntimeError(f"Base installer protocol mismatch. Update both firmware and stenosquid.py together. {header}")
        images = {}
        for row in rows:
            name, size, crc, verified = row.split()
            if name not in DEFAULT_IMAGES or name in images or verified not in ("0", "1"):
                raise RuntimeError(f"Invalid base metadata: {row}")
            images[name] = (int(size), int(crc), verified == "1")
        return header == "2 READY", images
    raise TimeoutError("The keyboard did not finish checking dictionary flash.")


def install_hid(console, repair_base=None):
    def send(command, timeout=30):
        reply = console.command("base_install " + command, timeout=timeout)
        if reply != "OK":
            raise RuntimeError(reply)

    _, images = hid_install_status(console)
    if repair_base not in (None, "ALL") and repair_base not in images:
        raise RuntimeError(f"{repair_base} is not configured in this firmware.")
    missing = {}
    for name, (size, crc, verified) in images.items():
        if repair_base not in (None, "ALL", name):
            continue
        if verified and (not repair_base or read_hid_crc(console, name) == crc):
            print(f"{name}: already installed")
            continue
        path = DEFAULT_IMAGES[name]
        if not path.exists():
            print(f"Preparing {name} from the pinned dictionary sources…", flush=True)
            prepare_base(name, (size, crc))
        image = read_image(path)
        if (len(image), zlib.crc32(image)) != (size, crc):
            # Keep custom and older build files intact. Try the pinned sources
            # in a separate directory before asking the keyboard to write.
            print(f"Preparing the firmware's {name} image from the pinned sources…", flush=True)
            with TemporaryDirectory() as directory:
                prepared = Path(directory) / path.name
                prepare_base(name, (size, crc), prepared)
                image = read_image(prepared)
        check_matches_firmware(image, name, size, crc, path)
        missing[name] = image

    for name, image in missing.items():
        print(f"{'Repairing' if repair_base else 'Installing'} {name}…", flush=True)
        operation = 'repair' if repair_base else 'begin'
        reply = console.command(f"base_install {operation} {name}", timeout=600 if repair_base else 30)
        if not reply.startswith('OK ') or not reply[3:].isdigit():
            raise RuntimeError(reply)
        start = int(reply[3:])
        if start > len(image) or (start != len(image) and start % CHUNK_BYTES):
            raise RuntimeError('Invalid resume offset from keyboard')
        try:
            for offset in range(start, len(image), CHUNK_BYTES):
                chunk = image[offset:offset + CHUNK_BYTES]
                end = offset + len(chunk)
                encoded = base64.b64encode(chunk).decode('ascii')
                for attempt in range(3):
                    try:
                        reply = console.command(f"base_install data {offset} {encoded}", timeout=30)
                        break
                    except TimeoutError:
                        if attempt == 2: raise
                if reply != f"OK {end}": raise RuntimeError(reply)
                if end == len(image) or end % (64 * 1024) == 0:
                    print(f"Write and read back: {end:,}/{len(image):,} bytes", flush=True)
            send("commit", timeout=120)
        except (Exception, KeyboardInterrupt) as error:
            try:
                send("abort", timeout=2)
            except Exception:
                pass
            if (not repair_base and isinstance(error, RuntimeError) and
                    str(error).startswith("ERR Base region holds different data")):
                raise RuntimeError(f"{name} conflicts with the image in flash. To replace just this base, "
                                   f"run: python3 stenosquid.py --repair --base {name.lower()}") from None
            raise

    ready, images = hid_install_status(console)
    if repair_base not in (None, "ALL") and images[repair_base][2] and not ready:
        print(f"{repair_base} verified. Run python3 stenosquid.py --install for the remaining bases.")
        return
    if not ready or not all(verified for _, _, verified in images.values()):
        raise RuntimeError("The keyboard did not map all verified base dictionaries.")
    print("Base dictionaries are installed and ready. You can start using Javelin.")


def read_hid_crc(console, name):
    reply = console.command(f"base_install verify {name}", timeout=120)
    state, _, value = reply.partition(" ")
    if state not in ("OK", "MISMATCH"):
        raise RuntimeError(f"{name}: {reply}. Check that Left-Default supports USB HID verification.")
    return int(value)


def inspect_hid(console, base, verify=False):
    ready, images = hid_install_status(console)
    names = list(images) if base == "ALL" else [base]
    if base != "ALL" and base not in images:
        raise RuntimeError(f"{base} is not configured in this firmware.")
    if not verify:
        for name in names:
            size, crc, verified = images[name]
            state = ("MAPPED" if ready else "VERIFIED") if verified else "UNPROVISIONED"
            print(f"{name}: {state}, {size:,} bytes, expected CRC32 {crc:08x}")
        return

    failed = False
    for name in names:
        crc = read_hid_crc(console, name)
        if crc == images[name][1]:
            print(f"{name}: OK, CRC32 {crc:08x}")
        else:
            print(f"{name}: FAILED, flash CRC32 {crc:08x}, expected {images[name][1]:08x}")
            failed = True
    if failed:
        raise SystemExit(1)


from serial_tools import find_port, LineReader


# Diagnostics commands in the logging build: letter sent, line that ends the reply.
# The third field is how long to wait for the reply, in seconds: the rail test
# takes about ten and the power-cycle bench about half a minute.
DIAG = {
    "status": (b"S", "END", 20),
    "log": (b"D", "LOG END", 20),
    "clear": (b"Z", "CLEARED", 20),
    "rail": (b"R", "RAIL END", 40),
    "bench": (b"T", "BENCH END", 90),
}


def diag(port, what):
    """Print the firmware's status, saved boot log, or clear both. Never prompts."""
    command, end, wait = DIAG[what]
    port.reset_input_buffer()
    port.write(command)
    port.flush()
    deadline = time.monotonic() + wait
    reader = LineReader()
    while time.monotonic() < deadline:
        raw = reader.read(port)
        if raw is None:
            continue
        line = raw.decode("utf-8", errors="replace").rstrip()
        if line:
            print(line, flush=True)
        if line.startswith(end):
            return
    raise SystemExit("No diagnostics reply. This needs the logging build with "
                     "CONFIG_ZMK_JAVELIN_DIAG, which the Left-Diagnostic builds have")



def handle_native(console, args, status):
    """Handle non-install Native operations on the existing USB connection."""
    from import_personal import parse_named, write_json

    if args.base or args.repair:
        raise ValueError("Native uses a shared pool. Base-specific repair is unavailable.")
    if args.status:
        print(status)
        if status.startswith(("NATIVE1 READY ", "NATIVE2 READY ")):
            print(console.command("list_dictionaries"))
        return
    if not status.startswith(("NATIVE1 READY ", "NATIVE2 READY ")):
        raise RuntimeError("Native dictionaries are unavailable. Run --install --confirm-erase to provision.")
    if args.verify:
        print("Verifying the full dictionary store. Wait for verification before typing.", flush=True)
        reply = console.command("user_dictionary_verify", timeout=180)
        if reply != "OK":
            raise RuntimeError(reply)
        print("Native dictionaries verified.")
    elif args.update:
        sys.path.insert(0, str(REPO / "tools/storage"))
        from native_base_update import replace_pinned

        catalog = json.loads(console.command("native_catalog"))
        changed, conflicts = replace_pinned(console, catalog)
        if not changed:
            print("Installed bases already match the pinned versions.")
        else:
            print(f"Updated {', '.join(changed)}. Retained {len(conflicts)} conflicting local edits.")
            if conflicts:
                print(json.dumps(conflicts, ensure_ascii=False, indent=2))
        return bool(changed)
    elif args.list_dictionaries:
        print(json.dumps(json.loads(console.command("native_catalog")), indent=2))
    elif args.backup_all:
        sys.path.insert(0, str(REPO / "tools/storage"))
        from native_update import backup_all
        from native_recovery import save_atomic

        backup = backup_all(console)
        save_atomic(args.backup_all, backup)
        print(f"Saved full dictionary backup to {args.backup_all}.")
    elif args.backup:
        name = args.dictionary or "user_dictionary"
        if args.dictionary:
            sys.path.insert(0, str(REPO / "tools/storage"))
            from native_update import export_dictionary

            entries, names = parse_named(json.dumps(export_dictionary(console, name)))
        else:
            entries, names = parse_named(console.command("print_dictionary user_dictionary", timeout=1800))
        if console.command("native_install status") != status:
            raise RuntimeError("Native dictionary state changed during export. No backup was written.")
        write_json(args.backup, entries, names)
        print(f"Saved {len(entries):,} entries from {name} to {args.backup}.")
    elif args.rename or args.delete:
        if console.command("native_install update manage-capabilities") != "OK MANAGE1":
            raise RuntimeError("Flash firmware with dictionary management support first.")
        sys.path.insert(0, str(REPO / "tools/storage"))
        from native_update import transfer

        return transfer(console, rename=args.rename, delete=args.delete)
    elif args.personal or args.compact or args.move:
        sys.path.insert(0, str(REPO / "tools/storage"))
        from native_manage import import_entries
        from native_update import transfer

        entries, names = parse_named(args.personal.read_text(encoding="utf-8")) if args.personal else ({}, {})
        state = json.loads(console.command("user_dictionary_status"))
        if not state.get("ready") or state.get("read_only") or not state.get("personal_id"):
            raise RuntimeError("Native dictionary is unavailable for writing")
        if args.name is not None or args.move:
            capabilities = console.command("native_install update named-capabilities").split()
            if len(capabilities) != 3 or capabilities[:2] != ["OK", "NAMED1"]:
                raise RuntimeError("Flash firmware with named dictionary support before using this command.")
            return transfer(console, entries, name=args.name, limit=int(capabilities[2]),
                            move=(args.move, args.position) if args.move else None)
        if args.compact or len(entries) >= 256:
            if console.command("native_install update capabilities") != "OK BULK1":
                raise RuntimeError("Flash Native firmware with staged updates before running this command.")
            return transfer(console, None if args.compact else entries)
        import_entries(console, state["personal_id"], entries, names)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("port", nargs="?", help="USB serial port for --diag only")
    parser.add_argument("--install", action="store_true", help="Install and verify base dictionaries over USB HID")
    parser.add_argument("--import", dest="personal", type=Path, metavar="USER.JSON", help="Merge a personal dictionary over USB HID")
    parser.add_argument("--as", dest="name", metavar="NAME", help="Import into a new named dictionary")
    parser.add_argument("--rename", nargs=2, metavar=("OLD", "NEW"), help="Rename an independent dictionary")
    parser.add_argument("--delete", metavar="NAME", help="Delete an independent dictionary")
    parser.add_argument("--manager", action="store_true", help="Open the local Steno Squid Manager")
    parser.add_argument("--move", metavar="NAME", help="Move a dictionary in the lookup priority list")
    parser.add_argument("--position", type=int, metavar="N", help="Priority position for --move, starting at 1")
    parser.add_argument("--compact", action="store_true", help="Reclaim partly stale Native journals over USB HID")
    parser.add_argument("--update", action="store_true", help="Update installed bases to the pinned revisions")
    parser.add_argument("--list-dictionaries", action="store_true", help="Show the Native dictionary catalog")
    parser.add_argument("--backup-all", type=Path, metavar="FILE.JSON", help="Save the Native catalog settings and overlays")
    parser.add_argument("--dictionary", metavar="NAME", help="Select a dictionary for --backup")
    parser.add_argument("--backup", type=Path, metavar="FILE.JSON", help="Export the personal dictionary over USB HID")
    parser.add_argument("--confirm-erase", action="store_true", help="Explicitly replace the Native dictionary pool")
    parser.add_argument("--restore-all", type=Path, metavar="FILE.JSON", help="Restore a full backup; replaces the dictionary pool")
    parser.add_argument("--restore", type=Path, metavar="BACKUP.JSON", help="Seed personal entries when provisioning Native")
    parser.add_argument("--serial", help="USB keyboard serial number (not a serial-port path)")
    parser.add_argument("--base", choices=("lapwing", "plover", "extras", "all"))
    parser.add_argument("--repair", action="store_true", help="Check and repair all bases, or select one with --base")
    parser.add_argument("--verify", action="store_true", help="Read and verify base dictionaries without writing")
    parser.add_argument("--status", action="store_true", help="Read dictionary status")
    parser.add_argument("--diag", nargs="?", const="report", choices=["report", *sorted(DIAG)])
    args = parser.parse_args()
    actions = (args.install, args.personal, args.backup, args.repair, args.verify, args.status, args.diag, args.compact, args.move, args.rename, args.delete, args.manager, args.backup_all, args.list_dictionaries, args.update, args.restore_all)
    if sum(map(bool, actions)) != 1:
        parser.error("Choose exactly one action (see --help)")
    if args.dictionary is not None and not args.backup:
        parser.error("--dictionary goes with --backup")
    if args.name is not None and not args.personal:
        parser.error("--as requires --import")
    if bool(args.move) != (args.position is not None) or (args.position is not None and args.position < 1):
        parser.error("--move requires --position with a positive position")
    if args.port and not args.diag:
        parser.error("Serial provisioning is retired. Omit the port to use USB HID; ports are only for --diag")
    if args.confirm_erase and not (args.install or args.restore_all):
        parser.error("--confirm-erase goes with --install or --restore-all")
    if args.restore and not args.install:
        parser.error("--restore goes with --install")
    if args.diag:
        if args.base or args.serial:
            parser.error("--diag stands alone")
        with Serial(args.port or find_port(), 115200, timeout=0.25, write_timeout=5, exclusive=True) as port:
            sections = ("status", "log", "rail", "bench") if args.diag == "report" else (args.diag,)
            for section in sections:
                if len(sections) > 1:
                    print(f"\n[{section}]", flush=True)
                diag(port, section)
        return
    if args.base and not (args.repair or args.verify or args.status):
        parser.error("--base goes with --repair, --verify or --status")
    if args.restore and not args.confirm_erase:
        parser.error("--restore requires --confirm-erase")
    if args.restore_all:
        if not args.confirm_erase:
            parser.error("--restore-all requires --confirm-erase")
        sys.path.insert(0, str(REPO / "tools/storage"))
        from native_restore import restore_all

        restore_all(args.serial, json.loads(args.restore_all.read_text(encoding="utf-8")))
        return
    if args.manager:
        sys.path.insert(0, str(REPO / "tools/manager"))
        from manager import serve

        serve(args.serial)
        return
    from import_personal import open_console

    reboot = False
    with open_console(args.serial) as console:
        native = console.command("native_install status")
        if native.startswith(("NATIVE1 ", "NATIVE2 ")):
            if not args.install:
                reboot = handle_native(console, args, native)
                if not reboot:
                    return
            else:
                if not args.confirm_erase:
                    if native.startswith(("NATIVE1 READY ", "NATIVE2 READY ")):
                        print("Native dictionaries are already installed.")
                        return
                    raise RuntimeError("Provisioning replaces the dictionary pool. Back up existing data first, "
                                       "then run --install --confirm-erase (optionally with --restore BACKUP.JSON).")
                if not native.startswith("NATIVE2 "):
                    raise RuntimeError("Flash the matching Native firmware before installing this image.")
        elif native == "ERR Command unavailable on this keyboard":
            if any((args.compact, args.move, args.rename, args.delete, args.name, args.dictionary, args.backup_all, args.list_dictionaries, args.update)):
                raise ValueError("This dictionary operation requires Native firmware.")
            if args.confirm_erase or args.restore:
                raise ValueError("These install options require Native firmware.")
            if not (args.backup or args.personal):
                base = (args.base or "all").upper()
                if args.verify or args.status:
                    inspect_hid(console, base, verify=args.verify)
                else:
                    install_hid(console, base if args.repair else None)
                return
        else:
            raise RuntimeError(f"Unrecognized dictionary installer response: {native}")

    if reboot:
        from native_install import wait_ready

        wait_ready(args.serial)
    elif args.install:
        sys.path.insert(0, str(REPO / "tools/storage"))
        from native_install import install

        install(args.serial, restore=args.restore)
    elif args.backup:
        from import_personal import backup_file

        backup_file(args.backup, args.serial)
    elif args.personal:
        from import_personal import import_file

        import_file(args.personal, args.serial)


if __name__ == "__main__":
    try:
        main()
    except (KeyError, ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(f"Error: {error}")
    except KeyboardInterrupt:
        sys.exit("Cancelled. Reconnect the keyboard and check the dictionary before retrying.")
