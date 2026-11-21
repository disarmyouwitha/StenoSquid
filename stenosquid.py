"""Install the base dictionaries, or import or back up a personal Plover JSON file over USB.

Use --install to provision the Native dictionary pool. Provisioning requires
explicit permission to erase the pool and can seed a personal JSON export.
"""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time


def Serial(*args, **kwargs):
    try:
        from serial import Serial as open_port
    except ImportError:
        raise SystemExit("Install the USB serial dependency with: python3 -m pip install pyserial") from None
    return open_port(*args, **kwargs)


REPO = Path(__file__).resolve().parent
DICTIONARY_TOOLS = REPO / "tools" / "dictionaries"
sys.path.insert(0, str(DICTIONARY_TOOLS))
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

    if args.status:
        print(status)
        if status.startswith("NATIVE2 READY "):
            print(console.command("list_dictionaries"))
        return
    if not status.startswith("NATIVE2 READY "):
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
    elif args.uninstall_factory or args.restore_factory:
        sys.path.insert(0, str(REPO / "tools/storage"))
        from native_base_update import factory_package

        package = args.uninstall_factory or args.restore_factory
        report = factory_package(console, package, restore=bool(args.restore_factory),
                                 confirmation=args.confirm_factory)
        print(json.dumps(report, indent=2))
        return bool(args.confirm_factory)
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

        if args.delete:
            from native_snapshot import change_snapshot

            catalog = json.loads(console.command("native_catalog"))
            selected = next((row for row in catalog if row["name"] == args.delete), None)
            if selected and selected["kind"] == "snapshot":
                return change_snapshot(console, "delete", args.delete, stage=True)
        return transfer(console, rename=args.rename, delete=args.delete)
    elif args.personal or args.compact or args.move:
        sys.path.insert(0, str(REPO / "tools/storage"))
        from native_manage import import_entries
        from native_update import transfer

        entries, names = parse_named(args.personal.read_text(encoding="utf-8")) if args.personal else ({}, {})
        state = json.loads(console.command("user_dictionary_status"))
        if not state.get("ready") or state.get("read_only") or not state.get("personal_id"):
            raise RuntimeError("Native dictionary is unavailable for writing")
        if args.name is not None:
            from native_import_plan import named_import

            named_import(console, args.name, entries, stage=True)
            return True
        if args.move:
            return transfer(console, move=(args.move, args.position))
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
    parser.add_argument("--as", dest="name", metavar="NAME", help="Import as a new named compact dictionary")
    parser.add_argument("--rename", nargs=2, metavar=("OLD", "NEW"), help="Rename an independent dictionary")
    parser.add_argument("--delete", metavar="NAME", help="Delete an independent dictionary")
    parser.add_argument("--manager", "--manage", action="store_true",
                        help="Open the local Steno Squid Manager (default when no action is given)")
    parser.add_argument("--move", metavar="NAME", help="Move a dictionary in the lookup priority list")
    parser.add_argument("--position", type=int, metavar="N", help="Priority position for --move, starting at 1")
    parser.add_argument("--compact", action="store_true", help="Reclaim partly stale Native journals over USB HID")
    parser.add_argument("--update", action="store_true", help="Update installed bases to the pinned revisions")
    sys.path.insert(0, str(REPO / "tools/storage"))
    from build_native_image import FACTORY_KEYS

    parser.add_argument("--uninstall-factory", choices=tuple(FACTORY_KEYS), help="Preview factory dictionary removal")
    parser.add_argument("--restore-factory", choices=tuple(FACTORY_KEYS), help="Preview installing a pinned dictionary")
    parser.add_argument("--confirm-factory", metavar="TOKEN", help="Apply the approved factory dictionary preview")
    parser.add_argument("--list-dictionaries", action="store_true", help="Show the Native dictionary catalog")
    parser.add_argument("--backup-all", type=Path, metavar="FILE.JSON", help="Save a complete Native recovery bundle")
    parser.add_argument("--dictionary", metavar="NAME", help="Select a dictionary for --backup")
    parser.add_argument("--backup", type=Path, metavar="FILE.JSON", help="Export the personal dictionary over USB HID")
    parser.add_argument("--confirm-erase", action="store_true", help="Explicitly replace the Native dictionary pool")
    parser.add_argument("--restore", type=Path, metavar="BACKUP.JSON", help="Seed personal entries when provisioning Native")
    parser.add_argument("--serial", help="USB keyboard serial number (not a serial-port path)")
    parser.add_argument("--verify", action="store_true", help="Read and verify base dictionaries without writing")
    parser.add_argument("--status", action="store_true", help="Read dictionary status")
    parser.add_argument("--diag", nargs="?", const="report", choices=["report", *sorted(DIAG)])
    args = parser.parse_args()
    actions = (args.install, args.personal, args.backup, args.verify, args.status, args.diag, args.compact, args.move, args.rename, args.delete, args.manager, args.backup_all, args.list_dictionaries, args.update, args.uninstall_factory, args.restore_factory)
    action_count = sum(map(bool, actions))
    if action_count > 1:
        parser.error("Choose exactly one action (see --help)")
    if action_count == 0:
        args.manager = True
    if args.dictionary is not None and not args.backup:
        parser.error("--dictionary goes with --backup")
    if args.name is not None and not args.personal:
        parser.error("--as requires --import")
    if bool(args.move) != (args.position is not None) or (args.position is not None and args.position < 1):
        parser.error("--move requires --position with a positive position")
    if args.port and not args.diag:
        parser.error("Serial provisioning is retired. Omit the port to use USB HID; ports are only for --diag")
    if args.confirm_factory is not None and (not (args.uninstall_factory or args.restore_factory)
                                              or len(args.confirm_factory) != 64):
        parser.error("--confirm-factory requires a 64-character preview token and a factory dictionary action")
    if args.confirm_erase and not args.install:
        parser.error("--confirm-erase goes with --install")
    if args.restore and not args.install:
        parser.error("--restore goes with --install")
    if args.diag:
        if args.serial:
            parser.error("--diag stands alone")
        with Serial(args.port or find_port(), 115200, timeout=0.25, write_timeout=5, exclusive=True) as port:
            sections = ("status", "log", "rail", "bench") if args.diag == "report" else (args.diag,)
            for section in sections:
                if len(sections) > 1:
                    print(f"\n[{section}]", flush=True)
                diag(port, section)
        return
    if args.restore and not args.confirm_erase:
        parser.error("--restore requires --confirm-erase")
    if args.manager:
        sys.path.insert(0, str(REPO / "tools/manager"))
        from manager import serve

        serve(args.serial)
        return
    from import_personal import open_console

    with open_console(args.serial) as console:
        native = console.command("native_install status")
        if not native.startswith("NATIVE2 "):
            raise RuntimeError("Flash the matching Native firmware before using dictionary commands.")
        if not args.install:
            reboot = handle_native(console, args, native)
        elif not args.confirm_erase:
            if native.startswith("NATIVE2 READY "):
                print("Native dictionaries are already installed.")
                return
            raise RuntimeError("Provisioning replaces the dictionary pool. Back up existing data first, "
                               "then run --install --confirm-erase (optionally with --restore BACKUP.JSON).")

    sys.path.insert(0, str(REPO / "tools/storage"))
    if args.install:
        from native_install import install

        install(args.serial, restore=args.restore)
    elif reboot:
        from native_install import wait_ready

        wait_ready(args.serial)


if __name__ == "__main__":
    try:
        main()
    except (KeyError, ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(f"Error: {error}")
    except KeyboardInterrupt:
        sys.exit("Cancelled. Reconnect the keyboard and check the dictionary before retrying.")
