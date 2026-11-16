"""Compile a Plover JSON dictionary and transfer one snapshot over USB HID."""

from contextlib import contextmanager
import json
from pathlib import Path
import struct
import sys
import time
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dict_compiler"))
from compile_dict import parse_outline

REPORT_BYTES = 63
CHUNK_BYTES = 256
HEADER_SECTOR_BYTES = 4096
USAGE = (0xFF31, 0x74)


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key {key!r}")
        result[key] = value
    return result


def parse_dictionary(source):
    data = json.loads(source, object_pairs_hook=unique_pairs)
    if not isinstance(data, dict):
        raise ValueError("The file must be a Plover JSON object mapping outlines to translations.")
    entries = {}
    for outline, translation in data.items():
        strokes = parse_outline(outline)
        if len(strokes) > 16:
            raise ValueError(f"{outline!r} has more than 16 strokes.")
        if not isinstance(translation, str) or "\0" in translation:
            raise ValueError(f"{outline!r} needs a text translation without NUL bytes.")
        text = translation.encode("utf-8")
        if len(text) > 2047:
            raise ValueError(f"{outline!r} has a translation longer than 2,047 UTF-8 bytes.")
        if strokes in entries and entries[strokes] != text:
            raise ValueError(f"{outline!r} uses the same keys as another outline with different text.")
        entries[strokes] = text
    return entries


def hash_bytes(data):
    value = 2166136261
    for byte in data:
        value = ((value ^ byte) * 16777619) & 0xFFFFFFFF
    return value


def compile_body(entries, slot_bytes):
    count = len(entries)
    used = HEADER_SECTOR_BYTES + 16 * count + sum(
        8 + 4 * len(strokes) + ((len(text) + 4) & ~3) for strokes, text in entries.items()
    )
    if used > slot_bytes:
        raise ValueError(f"The merged dictionary needs {used:,} bytes. The slot holds {slot_bytes:,}.")
    ordered = []
    for strokes, text in entries.items():
        key = struct.pack(f"<{len(strokes)}I", *strokes)
        order = (hash_bytes(key), len(strokes), strokes)
        ordered.append((order, key, text))
    ordered.sort()
    forward, reverse, records = bytearray(), [], bytearray()
    for order, key, text in ordered:
        offset = HEADER_SECTOR_BYTES + 16 * count + len(records)
        forward += struct.pack("<II", order[0], offset)
        reverse.append((hash_bytes(text), order, offset))
        records += struct.pack("<II", order[1], len(text) + 1) + key + text + b"\0"
        records += b"\xff" * (-len(records) % 4)
    backward = b"".join(struct.pack("<II", text_hash, off)
                          for text_hash, _, off in sorted(reverse))
    body = bytes(forward) + backward + records
    return body, max((len(strokes) for strokes in entries), default=0)


class Console:
    def __init__(self, device):
        self.device = device
        self.channel = 0

    def command(self, text, timeout=30):
        self.channel = (self.channel + 1) % 100
        prefix = f"c{self.channel:02} ".encode()
        line = text.encode() + b"\n"
        payload = REPORT_BYTES - len(prefix)
        for offset in range(0, len(line), payload):
            report = (prefix + line[offset:offset + payload]).ljust(REPORT_BYTES, b"\0")
            if self.device.write(b"\0" + report) <= 0:
                raise RuntimeError("USB HID write failed. Keep the left half connected.")
        response = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            packet = self.device.read(REPORT_BYTES, 250)
            if packet:
                response.extend(bytes(packet).rstrip(b"\0"))
                if response.endswith(b"\n\n"):
                    if response.startswith(prefix):
                        return response[len(prefix):].decode().rstrip("\n")
                    response.clear()
        raise TimeoutError(f"No reply to {text.split()[0]}. Reconnect USB and check the keyboard.")

    def import_command(self, text, timeout=30):
        reply = self.command("user_dictionary_import " + text, timeout)
        if reply != "OK":
            raise RuntimeError(reply)


@contextmanager
def open_console(serial=None):
    try:
        import hid
    except ImportError:
        raise SystemExit("Install the USB HID dependency with: python3 -m pip install hidapi") from None
    devices = [
        device for device in hid.enumerate()
        if (device.get("usage_page"), device.get("usage")) == USAGE
        and device.get("bus_type", 0) in (0, 1)  # HIDAPI unknown or USB
        and (device.get("serial_number") == serial if serial else
             device.get("serial_number") and device.get("product_string") == "Steno Squid")
    ]
    if len(devices) != 1:
        raise RuntimeError("Connect one Steno Squid left half by USB, or use --serial to select it.")
    device = hid.device()
    device.open_path(devices[0]["path"])
    try:
        yield Console(device)
    finally:
        device.close()


def import_file(path, serial=None):
    entries = parse_dictionary(path.read_text(encoding="utf-8-sig"))
    if not entries:
        raise ValueError("The JSON file has no entries.")
    with open_console(serial) as console:
        status = json.loads(console.command("user_dictionary_status"))
        if status.get("bulk_import") != 1:
            raise RuntimeError("Install a left firmware build that supports bulk import.")
        if not status["available"]:
            raise RuntimeError("The onboard personal dictionary is unavailable.")
        existing = parse_dictionary(console.command("print_dictionary user_dictionary", 600))
        if len(existing) != status["entries"]:
            raise RuntimeError("The keyboard returned an incomplete dictionary export. Try again.")
        replacements = sum(key in existing and existing[key] != value
                           for key, value in entries.items())
        additions = len(entries.keys() - existing.keys())
        if not additions and not replacements:
            print("All entries already match the keyboard.")
            return
        existing.update(entries)
        body, maximum = compile_body(existing, status["slot_bytes"])
        print(f"Adding {additions:,}, replacing {replacements:,}, saving {len(existing):,} entries.",
              flush=True)
        console.import_command(f"begin {status['generation']} {len(existing)} "
                               f"{len(body)} {maximum} {zlib.crc32(body)}")
        try:
            for offset in range(0, len(body), CHUNK_BYTES):
                chunk = body[offset:offset + CHUNK_BYTES]
                console.import_command(f"data {offset} {chunk.hex()}")
                end = offset + len(chunk)
                if end == len(body) or end % (64 * 1024) == 0:
                    print(f"Sent {end:,}/{len(body):,} bytes", flush=True)
            print("Verifying and committing the snapshot…", flush=True)
            console.import_command("commit", 600)
        except (Exception, KeyboardInterrupt):
            try:
                console.import_command("abort", 2)
            except Exception:
                pass
            raise
        print("Personal dictionary saved and verified.")
