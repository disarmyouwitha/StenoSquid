"""Install the base dictionaries, or import or back up a personal Plover JSON file over USB.

With no options it installs every image the firmware knows that is not
installed yet, from the files under build/. Name one image with --base to
act on it alone; --repair and --verify are the usual reasons to.
"""

import argparse
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
        if header == "1 LOADING":
            time.sleep(0.25)
            continue
        if header == "1 ERROR":
            raise RuntimeError("The keyboard could not bring up dictionary flash. Check its connection and power.")
        if header not in ("1 INSTALL", "1 READY") or not rows:
            raise RuntimeError(f"Base install unavailable. Use a current Left-Default or Left-No-Display build. {header}")
        images = {}
        for row in rows:
            name, size, crc, verified = row.split()
            if name not in DEFAULT_IMAGES or name in images or verified not in ("0", "1"):
                raise RuntimeError(f"Invalid base metadata: {row}")
            images[name] = (int(size), int(crc), verified == "1")
        return header == "1 READY", images
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
        if repair_base:
            print(f"Repairing {name}. Only its base flash region will be erased.", flush=True)
            send(f"repair {name}")
        else:
            print(f"Installing {name}…", flush=True)
            send(f"begin {name}")
        try:
            for stage in ("Preflight", "Write and read back"):
                for offset in range(0, len(image), CHUNK_BYTES):
                    chunk = image[offset:offset + CHUNK_BYTES]
                    end = offset + len(chunk)
                    erasing = repair_base and stage == "Preflight" and end == len(image)
                    if erasing:
                        print("Checking replacement checksum and erasing the selected base…", flush=True)
                    send(f"data {offset} {chunk.hex()}", timeout=600 if erasing else 30)
                    if end == len(image) or end % (64 * 1024) == 0:
                        print(f"{stage}: {end:,}/{len(image):,} bytes", flush=True)
            send("commit", timeout=120)
        except (Exception, KeyboardInterrupt) as error:
            try:
                send("abort", timeout=2)
            except Exception:
                pass
            if (not repair_base and isinstance(error, RuntimeError) and
                    str(error).startswith("ERR Base region holds different data")):
                raise RuntimeError(
                    f"{name} conflicts with the image in flash. To replace just this base, "
                    f"run: python3 stenosquid.py --repair --base {name.lower()}"
                ) from None
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("port", nargs="?", help="USB serial port for --diag only")
    parser.add_argument("--install", action="store_true", help="Install and verify base dictionaries over USB HID")
    parser.add_argument("--import", dest="personal", type=Path, metavar="USER.JSON", help="Merge a personal dictionary over USB HID")
    parser.add_argument("--backup", type=Path, metavar="FILE.JSON", help="Export the personal dictionary over USB HID")
    parser.add_argument("--serial", help="USB keyboard serial number (not a serial-port path)")
    parser.add_argument("--base", choices=("lapwing", "plover", "extras", "all"))
    parser.add_argument("--repair", action="store_true", help="Check and repair all bases, or select one with --base")
    parser.add_argument("--verify", action="store_true", help="Read and verify base dictionaries without writing")
    parser.add_argument("--status", action="store_true", help="Read dictionary status")
    parser.add_argument("--diag", nargs="?", const="report", choices=["report", *sorted(DIAG)])
    args = parser.parse_args()
    actions = (args.install, args.personal, args.backup, args.repair, args.verify, args.status, args.diag)
    if sum(map(bool, actions)) != 1:
        parser.error("Choose one action: --install, --import, --backup, --repair, --verify, --status or --diag")
    if args.port and not args.diag:
        parser.error("Serial provisioning is retired. Omit the port to use USB HID; ports are only for --diag")
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
    if (args.install or args.personal or args.backup) and args.base:
        parser.error("--base goes with --repair, --verify or --status")
    if args.backup:
        from import_personal import backup_file
        backup_file(args.backup, args.serial)
    elif args.personal:
        from import_personal import import_file
        import_file(args.personal, args.serial)
    else:
        from import_personal import open_console
        with open_console(args.serial) as console:
            base = (args.base or "all").upper()
            if args.verify or args.status:
                inspect_hid(console, base, verify=args.verify)
            else:
                install_hid(console, base if args.repair else None)


if __name__ == "__main__":
    try:
        main()
    except (KeyError, ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(f"Error: {error}")
    except KeyboardInterrupt:
        sys.exit("Cancelled. Reconnect the keyboard and check the dictionary before retrying.")
