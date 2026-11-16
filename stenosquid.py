"""Install the base dictionaries or import a personal Plover JSON file over USB.

With no options it installs every image the firmware knows that is not
installed yet, from the files under build/. Name one image with --base to
act on it alone; --repair and --verify are the usual reasons to.
"""

import argparse
import hashlib
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import zlib

from urllib.request import urlopen


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
# Serial command letters understood by the firmware, by base.
COMMANDS = {
    "LAPWING": {"query": b"?", "program": b"L", "erase": b"E", "verify": b"V", "info": b"I"},
    "PLOVER": {"query": b"p", "program": b"P", "erase": b"e", "verify": b"v", "info": b"i"},
    # One image holding the Lapwing commands, numbers, UK additions and proper nouns.
    "EXTRAS": {"query": b"x", "program": b"X", "erase": b"K", "verify": b"k", "info": b"n"},
}


def response(port, timeout=30, base="LAPWING"):
    deadline = time.monotonic() + timeout
    pending = b""
    while time.monotonic() < deadline:
        pending += port.readline()
        if not pending.endswith(b"\n"):
            continue
        line = pending.decode("utf-8", errors="replace").strip()
        pending = b""
        if not line:
            continue
        # Diagnostics can precede a reply on the same line.
        match = re.search(re.escape(base) + r" ([A-Z_]+) (\d+)", line)
        if match:
            state, value = match.groups()
            if state == "FLASH_CONFLICT":
                raise RuntimeError(
                    f"The flash already holds different data at {int(value):#x}. "
                    "Run again with --repair to erase this base first.")
            if state.endswith("ERROR"):
                raise RuntimeError(f"Device reported {state} at value {int(value):#x}")
            return state, int(value)
        print(line, flush=True)
    raise TimeoutError("No dictionary reply on USB serial")


def ask(port, base, command, expected, drain=True):
    """Send a command and return its first reply, which must be `expected`."""
    # The initial ZMK boot log can fill the CDC transmit buffer before DTR.
    for attempt in range(4):
        port.write(COMMANDS[base][command])
        try:
            state, value = response(port, 2, base)
        except TimeoutError:
            continue
        if expected is not None and state != expected:
            raise RuntimeError(f"Unexpected reply {state}")
        if drain and attempt:
            # An earlier attempt may still be answered. Drop its reply, or the
            # next command would read it as its own.
            time.sleep(0.5)
            port.reset_input_buffer()
        return state, value
    raise TimeoutError("The firmware did not answer. It may predate this command.")


def query(port, base="LAPWING"):
    return ask(port, base, "query", None)


def expected_image(port, base):
    """The size and CRC32 of the image this firmware was built for."""
    _, size = ask(port, base, "info", "SIZE", drain=False)
    state, crc = response(port, 2, base)
    if state != "CRC":
        raise RuntimeError(f"Unexpected reply {state}")
    # A retried request leaves a second answer on its way. Drop it.
    time.sleep(0.5)
    port.reset_input_buffer()
    return size, crc


def transfer(port, image, stage, base="LAPWING"):
    for offset in range(0, len(image), CHUNK_BYTES):
        chunk = image[offset:offset + CHUNK_BYTES]
        port.write(chunk)
        end = offset + len(chunk)
        if response(port, 10, base) != ("ACK", end):
            raise RuntimeError(f"Unexpected acknowledgment for {stage} at {offset:#x}")
        if end == len(image) or end % (256 * 1024) == 0:
            print(f"{stage}: {end:,}/{len(image):,} bytes", flush=True)


def program(port, image, base):
    size = len(image)
    port.write(COMMANDS[base]["program"])
    if response(port, base=base) != ("PREFLIGHT", size):
        raise RuntimeError("Device did not start the preflight")
    transfer(port, image, "Preflight", base)
    if response(port, base=base) != ("WRITE", size):
        raise RuntimeError("Device did not accept the preflight")
    transfer(port, image, "Program and read back", base)
    if response(port, 90, base) not in (("MAPPED", size), ("VERIFIED", size)):
        raise RuntimeError("Device did not verify the mapped dictionary")
    print("Dictionary verified. Query status for MAPPED once every image is provisioned.")


def verify(port, base):
    """Full CRC check of one base. Reads only. Returns True when it matches."""
    port.write(COMMANDS[base]["verify"])
    state, value = response(port, 120, base)
    if state == "VERIFY_OK":
        print(f"{base}: OK, CRC32 {value:08x}")
        return True
    if state == "VERIFY_MISMATCH":
        print(f"{base}: FAILED, the flash holds CRC32 {value:08x}, not the expected image")
    elif state == "VERIFY_READ_FAILED":
        print(f"{base}: FAILED, read error at offset {value:#x}")
    else:
        print(f"{base}: FAILED, unexpected reply {state}")
    return False


def repair(port, image, base):
    # Check the replacement before touching the flash.
    size, crc = expected_image(port, base)
    if len(image) != size or zlib.crc32(image) != crc:
        raise RuntimeError(
            f"This is not the image the firmware was built for. It expects {size:,} bytes "
            f"with CRC32 {crc:08x}; the file has {len(image):,} bytes with CRC32 "
            f"{zlib.crc32(image):08x}. Nothing was erased.")
    print(f"{base} image matches the firmware: {size:,} bytes, CRC32 {crc:08x}")
    print("Erasing", flush=True)
    port.write(COMMANDS[base]["erase"])
    while True:
        state, value = response(port, 15, base)
        if state == "ERASED":
            break
        if state != "ERASE":
            raise RuntimeError(f"Unexpected reply {state} during the erase")
        if value % (256 * 1024) == 0:
            print(f"Erase: {value:,} bytes", flush=True)
    program(port, image, base)


def read_image(path):
    """A JSC4 image file, or an error that says how to build it."""
    if not path.is_file():
        raise SystemExit(f"{path} not found. Build it with the matching prepare script first.")
    image = path.read_bytes()
    if image[:4] != b"JSC4" or len(image) % 4:
        raise SystemExit(f"{path} is not a word-aligned JSC4 image")
    return image


def present(port, name):
    """The size and CRC32 the firmware expects for `name`, or None if this firmware has no such image."""
    try:
        return expected_image(port, name)
    except TimeoutError:
        return None


def check_matches_firmware(image, name, size, crc, path):
    if len(image) != size or zlib.crc32(image) != crc:
        raise SystemExit(
            f"{path} is not the {name} image this firmware was built for. It expects "
            f"{size:,} bytes with CRC32 {crc:08x}; the file has {len(image):,} bytes with "
            f"CRC32 {zlib.crc32(image):08x}. Rebuild the image, or flash the firmware that "
            "was built with it.")


def upload_all(port, paths):
    """Install every image the firmware knows that is not installed yet."""
    seen = 0
    for name in COMMANDS:
        expected = present(port, name)
        if expected is None:
            print(f"{name}: not part of this firmware, skipped")
            continue
        seen += 1
        size, crc = expected
        state, _ = query(port, name)
        if state in ("MAPPED", "VERIFIED"):
            print(f"{name}: already installed")
            continue
        if state != "UNPROVISIONED":
            raise SystemExit(f"{name}: unexpected starting state {state}. "
                             f"Run with --repair --base {name.lower()} to erase and write it.")
        image = read_image(paths[name])
        check_matches_firmware(image, name, size, crc, paths[name])
        print(f"{name} image: {size:,} bytes, CRC32 {crc:08x}", flush=True)
        program(port, image, name)
    if not seen:
        raise SystemExit("The firmware reported no dictionary images")


def prepare_base(name, expected=None, output_path=None):
    path = output_path or DEFAULT_IMAGES[name]
    env = os.environ.copy()
    # Python.org macOS installs may lack an OpenSSL certificate bundle.
    system_certs = Path("/etc/ssl/cert.pem")
    context = None
    if (sys.platform == "darwin" and not ssl.get_default_verify_paths().cafile
            and not env.get("SSL_CERT_FILE") and system_certs.is_file()):
        env["SSL_CERT_FILE"] = str(system_certs)
        context = ssl.create_default_context(cafile=system_certs)
    if name in ("PLOVER", "EXTRAS"):
        subprocess.run([sys.executable, str(DICTIONARY_TOOLS / f"prepare_{name.lower()}.py"),
                        "--output-dir", str(path.parent)], check=True, env=env)
        return

    from prepare_extras import URL
    path.parent.mkdir(parents=True, exist_ok=True)
    source_path = path.with_suffix(".json")
    with urlopen(URL + source_path.name, timeout=60, context=context) as response:
        source = response.read()
    if hashlib.sha256(source).hexdigest() != "2750c20466bc5040d69167e02095776531afb6c3c1f6f662bba01006e7e0e484":
        raise RuntimeError("Pinned Lapwing source checksum mismatch.")
    source_path.write_bytes(source)
    command = [sys.executable, str(REPO / "tools/dict_compiler/compile_dict.py"),
               str(source_path), "--base-addr", "0x12000000", "-o", str(path)]
    subprocess.run(command + ["--name", "lapwing-base"], check=True)
    image = path.read_bytes()
    if expected and (len(image), zlib.crc32(image)) != expected:
        # Local builds have also used the display name inside the same image.
        fallback = path.with_suffix(".alternate.bin")
        try:
            subprocess.run(command + ["-n", "Lapwing Base", "-o", str(fallback)], check=True)
            candidate = fallback.read_bytes()
            if (len(candidate), zlib.crc32(candidate)) == expected:
                fallback.replace(path)
        finally:
            fallback.unlink(missing_ok=True)


def install(port):
    # Validate every required build image before beginning the first write.
    missing = {}
    for name, path in DEFAULT_IMAGES.items():
        expected = present(port, name)
        if expected is None:
            raise RuntimeError(f"Could not read {name} metadata. Flash the matching Left-Diagnostic UF2 and reconnect USB.")
        state, _ = query(port, name)
        if state in ("MAPPED", "VERIFIED"):
            print(f"{name}: already installed")
            continue
        if state != "UNPROVISIONED":
            raise RuntimeError(f"{name}: {state}. Use --repair --base {name.lower()} with the matching image.")
        if not path.exists():
            print(f"Preparing {name} from the pinned dictionary sources…", flush=True)
            prepare_base(name, expected)
        image = read_image(path)
        check_matches_firmware(image, name, *expected, path)
        missing[name] = image
    for name, image in missing.items():
        print(f"Installing {name}…", flush=True)
        program(port, image, name)
    print("Verifying the three base images…", flush=True)
    for name in DEFAULT_IMAGES:
        if not verify(port, name):
            raise RuntimeError(f"{name} verification failed. Use --repair --base {name.lower()}.")
    print("Base dictionaries are ready. Flash the everyday Left-Default UF2 (or Left-No-Display without a nice!view).")


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


def find_port():
    try:
        from serial.tools import list_ports
    except ImportError:
        raise SystemExit("Install the USB serial dependency with: python3 -m pip install pyserial") from None
    ports = list(list_ports.comports())
    matches = [port for port in ports if port.product == "Steno Squid"]
    if len(matches) == 1:
        return matches[0].device
    available = ", ".join(port.device for port in (matches or ports)) or "none"
    raise RuntimeError(f"Connect one left half running a Left-Diagnostic build, or supply its serial port. Available: {available}.")


def status(port):
    seen = 0
    for name in COMMANDS:
        if present(port, name) is None:
            continue
        seen += 1
        state, size = query(port, name)
        print(f"{name}: {state}, {size:,} bytes")
    if not seen:
        raise SystemExit("The firmware reported no dictionary images")


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
    pending = b""
    while time.monotonic() < deadline:
        pending += port.readline()
        if not pending.endswith(b"\n"):
            continue
        line = pending.decode("utf-8", errors="replace").rstrip()
        pending = b""
        if line:
            print(line, flush=True)
        if line.startswith(end):
            return
    raise SystemExit("No diagnostics reply. This needs the logging build with "
                     "CONFIG_ZMK_JAVELIN_DIAG, which the Left-Diagnostic builds have")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("port", nargs="?", help="USB serial device path for advanced maintenance commands")
    parser.add_argument("--install", action="store_true",
                        help="Install and verify bundled dictionaries over wired USB HID")
    parser.add_argument("--import", dest="personal", type=Path, metavar="USER.JSON",
                        help="Merge a Plover JSON file into the connected keyboard over USB HID")
    parser.add_argument("--serial", help="Select a wired USB keyboard by serial number")
    parser.add_argument("image", type=Path, nargs="?",
                        help="JSC4 image for a single --base. Default: the file under build/ "
                             "that the prepare script wrote")
    parser.add_argument("--base", choices=("lapwing", "plover", "extras", "all"),
                        help="Image to act on. With no --base, an upload installs every "
                             "image that is not installed yet and --verify checks all of "
                             "them. Over USB HID, --repair checks all bases by default")
    parser.add_argument("--repair", action="store_true",
                        help="Check and repair all bundled bases over wired USB HID, or select one with --base. "
                             "With a serial port, --base selects one base for a forced rewrite.")
    parser.add_argument("--verify", action="store_true",
                        help="Read the whole image and check its CRC. Writes nothing")
    parser.add_argument("--status", action="store_true",
                        help="Print the state of every image and exit")
    parser.add_argument("--diag", nargs="?", const="report", choices=["report", *sorted(DIAG)],
                        help="Collect flash status, rail and wake tests, and the boot log. "
                             "Pass a test name to run it alone.")
    args = parser.parse_args()
    if args.install or args.personal:
        if args.install and args.personal or any((args.base, args.image, args.repair, args.verify,
                                                 args.status, args.diag)):
            parser.error("--install and --import are standalone actions")
    if args.serial and (args.port or not (args.personal or args.install or args.repair or
                                          args.verify or args.status)):
        parser.error("--serial selects a keyboard for a wired USB HID command")
    if args.personal:
        if args.port:
            parser.error("--import uses USB HID; omit the serial port path")
        from import_personal import import_file
        import_file(args.personal, args.serial)
        return
    if sum([args.repair, args.verify, args.status]) > 1:
        parser.error("Use only one of --repair, --verify and --status")
    base = (args.base or "all").upper()
    if args.repair and args.port and base == "ALL":
        parser.error("Serial --repair needs --base lapwing, plover or extras")

    if args.install and args.port:
        with Serial(args.port, 115200, timeout=0.25, write_timeout=5,
                    exclusive=True) as port:
            install(port)
        return
    if not args.port and (args.install or args.repair or args.verify or args.status):
        if args.image or args.diag:
            parser.error("Use a serial port for a custom image or diagnostics")
        from import_personal import open_console
        try:
            with open_console(args.serial) as console:
                if args.verify or args.status:
                    inspect_hid(console, base, verify=args.verify)
                else:
                    install_hid(console, base if args.repair else None)
        except (OSError, ValueError, RuntimeError) as error:
            raise SystemExit(str(error)) from None
        return
    if args.diag:
        if args.repair or args.verify or args.status or args.image or args.base:
            parser.error("--diag stands alone")
        with Serial(args.port or find_port(), 115200, timeout=0.25,
                    write_timeout=5, exclusive=True) as port:
            sections = ("status", "log", "rail", "bench") if args.diag == "report" else (args.diag,)
            for section in sections:
                if len(sections) > 1:
                    print(f"\n[{section}]", flush=True)
                diag(port, section)
        return
    if not args.port:
        parser.error("Specify --install, --repair, --verify, --status, --import USER.JSON, or --diag")
    if args.image and base == "ALL":
        parser.error("An image path goes with a single --base")
    if args.image and (args.verify or args.status):
        parser.error("--verify and --status take no image")

    paths = dict(DEFAULT_IMAGES)
    if args.image:
        paths[base] = args.image

    with Serial(args.port, 115200, timeout=0.25, write_timeout=5, exclusive=True) as port:
        if args.status:
            status(port)
            return

        if args.verify:
            bases = list(COMMANDS) if base == "ALL" else [base]
            # Wait out the boot log before the long read.
            expected_image(port, bases[0])
            results = []
            for name in bases:
                if len(bases) > 1 and name != bases[0] and present(port, name) is None:
                    # A firmware built without this image stays silent.
                    print(f"{name}: not part of this firmware, skipped")
                    continue
                results.append(verify(port, name))
            sys.exit(0 if all(results) else 1)

        if args.repair:
            repair(port, read_image(paths[base]), base)
            return

        if base == "ALL":
            upload_all(port, paths)
            return

        state, size = query(port, base)
        image = read_image(paths[base])
        if size != len(image):
            raise SystemExit(f"Firmware expects {size:,} bytes")
        if state in ("MAPPED", "VERIFIED"):
            print("Dictionary is already verified. Use --repair to erase and write it again.")
            return
        if state != "UNPROVISIONED":
            raise SystemExit(f"Unexpected starting state {state}")

        print(f"{base} image: {len(image):,} bytes, CRC32 {zlib.crc32(image):08x}", flush=True)
        program(port, image, base)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(f"Error: {error}")
    except KeyboardInterrupt:
        sys.exit("Cancelled. Reconnect the keyboard and check the dictionary before retrying.")
