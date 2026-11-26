#!/usr/bin/env python3
"""Lab tool: install or uninstall factory dictionaries with the JAVASCRIPT transfer client on a real keyboard (USB).

Python does the planning (as today) and relays HID reports; the transfer protocol itself runs in Node using the same
tools/manager/browser/transfer_client.js the Manager will ship. Relaying through pipes adds latency a browser would
not have, so rates here are a lower bound for the JS path, not a Chrome measurement.

    python3 tools/bench/run_js_upload_hw.py lapwing-base         # install (restores a factory dictionary)
    python3 tools/bench/run_js_upload_hw.py -lapwing-base        # a leading '-' uninstalls

Needs node on PATH and a left half with the transfer option (CONFIG_ZMK_STENO_SQUID_TRANSFER_SESSION) over USB.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
for sub in ("tools/storage", "tools/dictionaries", "tools/dict_compiler"):
    sys.path.insert(0, str(ROOT / sub))
import native_base_update
from import_personal import open_console

SCRIPT = ROOT / "tools/bench/js/hw_upload.mjs"
LAST = {}


def read_exact(stream, count):
    data = b""
    while len(data) < count:
        chunk = stream.read(count - len(data))
        if not chunk:
            raise RuntimeError("The JavaScript client exited early.")
        data += chunk
    return data


def js_publish(console, update, progress):
    with tempfile.TemporaryDirectory(prefix="steno-js-hw-") as folder:
        spec = Path(folder) / "update.json"
        spec.write_text(json.dumps({"segments": [[segment, block.hex()] for segment, block in update.segments.items()],
                                    "metadata": update.metadata, "payload": update.payload.hex()}))
        process = subprocess.Popen(["node", str(SCRIPT), str(spec)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
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
                elif tag == b"C":
                    text = read_exact(process.stdout, struct.unpack("<H", read_exact(process.stdout, 2))[0]).decode()
                    reply = console.command(text, timeout=10).encode()
                    process.stdin.write(b"C" + struct.pack("<H", len(reply)) + reply)
                elif tag == b"D":
                    result = json.loads(read_exact(process.stdout, struct.unpack("<I", read_exact(process.stdout, 4))[0]))
                    break
                else:
                    raise RuntimeError(f"Unexpected message {tag!r} from the JavaScript client.")
        finally:
            process.stdin.close()
            process.wait(timeout=30)
    LAST.clear()
    LAST.update(result)
    if "error" in result:
        raise RuntimeError(result["error"])
    for message in result["messages"]:
        progress(message)


@contextmanager
def open_when_ready(limit=300):
    """open_console() is itself a context manager, so entering it is what must be retried."""
    begun = time.time()
    while True:
        manager = open_console()
        try:
            console = manager.__enter__()
            break
        except RuntimeError:
            if time.time() - begun > limit:
                raise
            time.sleep(0.5)
    try:
        yield console
    finally:
        manager.__exit__(None, None, None)


def main():
    native_base_update.publish = js_publish
    for argument in sys.argv[1:]:
        restore = not argument.startswith("-")
        package = argument.lstrip("-")
        print(f"== {'install' if restore else 'UNINSTALL'} {package} (JavaScript client)", flush=True)
        LAST.clear()
        with open_when_ready() as console:
            report = native_base_update.factory_package(console, package, restore=restore, progress=lambda _: None)
            native_base_update.factory_package(console, package, restore=restore, confirmation=report["confirmation"],
                                              progress=lambda _: None)
        stats = LAST["stats"]
        seconds = stats["totalMs"] / 1000
        print(f"   stages: begin {stats['beginMs']:.0f} ms | stream {stats['streamMs'] / 1000:.2f} s | "
              f"verify {stats['verifyMs'] / 1000:.2f} s | commit {stats['commitMs'] / 1000:.2f} s | total {seconds:.2f} s")
        print(f"   frames {stats['dataFrames']}, acks {stats['acks']}, naks {stats['naks']}, rewinds {stats['rewinds']}, "
              f"stalls {stats['stalls']}; {stats['streamBytes'] / stats['streamMs'] * 1000 / 1024:.1f} KiB/s of record bytes over the stream stage")
        if any(LAST.get("dropped", {}).values()):
            print(f"   injected loss (STENO_DROP_WRITES / STENO_DROP_READS): {LAST['dropped']}")
        time.sleep(2)
    print("done")


if __name__ == "__main__":
    main()
