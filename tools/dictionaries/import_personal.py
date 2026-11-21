"""USB HID console and Plover JSON codec for the Native dictionary tools."""

from contextlib import contextmanager
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dict_compiler"))
from compile_dict import parse_outline

REPORT_BYTES = 63
USAGE = (0xFF31, 0x74)


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key {key!r}")
        result[key] = value
    return result


def parse_named(source):
    """The entries of a Plover JSON object, and the outline text of each."""
    data = json.loads(source, object_pairs_hook=unique_pairs)
    if not isinstance(data, dict):
        raise ValueError("The file must be a Plover JSON object mapping outlines to translations.")
    entries, names = {}, {}
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
        names.setdefault(strokes, outline)
    return entries, names


class Console:
    def __init__(self, device, serial=None):
        self.device = device
        self.serial = serial
        self.channel = 0

    def command(self, text, timeout=30):
        deadline = time.monotonic() + timeout
        while True:
            reply = self._exchange(text, deadline)
            if reply != "ERR Native flash busy" or not text.startswith("native_install "):
                return reply
            if time.monotonic() >= deadline:
                return reply
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
            if time.monotonic() >= deadline:
                return reply

    def _exchange(self, text, deadline):
        self.channel = (self.channel + 1) % 100
        prefix = f"c{self.channel:02} ".encode()
        line = text.encode() + b"\n"
        payload = REPORT_BYTES - len(prefix)
        for offset in range(0, len(line), payload):
            report = (prefix + line[offset:offset + payload]).ljust(REPORT_BYTES, b"\0")
            if self.device.write(b"\0" + report) <= 0:
                raise RuntimeError("USB HID write failed. Keep the left half connected.")
        response = bytearray()
        while time.monotonic() < deadline:
            packet = self.device.read(REPORT_BYTES, 250)
            if packet:
                response.extend(bytes(packet).rstrip(b"\0"))
                if response.endswith(b"\n\n"):
                    if response.startswith(prefix):
                        return response[len(prefix):].decode().rstrip("\n")
                    response.clear()
        raise TimeoutError(f"No reply to {text.split()[0]}. Reconnect USB and check the keyboard.")



def usb_console_devices(serial=None):
    try:
        import hid
    except ImportError:
        raise SystemExit("Install the USB HID dependency with: python3 -m pip install hidapi") from None
    return [
        device for device in hid.enumerate()
        if (device.get("usage_page"), device.get("usage")) == USAGE
        and device.get("bus_type", 0) in (0, 1)  # HIDAPI unknown or USB
        and (device.get("serial_number") == serial if serial else
             device.get("serial_number") and device.get("product_string") == "Steno Squid")
    ]


@contextmanager
def open_console(serial=None):
    devices = usb_console_devices(serial)
    if len(devices) != 1:
        raise RuntimeError("Connect one Steno Squid left half by USB, or use --serial to select it.")
    import hid

    device = hid.device()
    device.open_path(devices[0]["path"])
    try:
        yield Console(device, devices[0]["serial_number"])
    finally:
        device.close()


def write_json(path, entries, names):
    """Write a Plover JSON file. Never replaces an existing file."""
    data = {names[strokes]: text.decode("utf-8") for strokes, text in entries.items()}
    with open(path, "x", encoding="utf-8", newline="\n") as file:
        json.dump(data, file, ensure_ascii=False, indent=0)
        file.write("\n")
