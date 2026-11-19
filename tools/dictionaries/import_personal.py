"""Import, merge and back up the personal dictionary over USB HID.

A small change is sent as individual add_user_entry commands. A large one is
built into whole flash segments on the host (segment_image.py) and uploaded.
"""

from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dict_compiler"))
from compile_dict import parse_outline
from segment_image import Image

REPORT_BYTES = 63
CHUNK_BYTES = 512
# Fewer differing entries than this are added one by one.
SMALL_CHANGE = 500
# The keyboard's console line buffer is 2,048 bytes.
LINE_BYTES = 1900
# Segments the keyboard keeps for itself after a reset: the meta log, one
# segment for each table and the cleaning reserve of three.
OVERHEAD_SEGMENTS = 6
USAGE = (0xFF31, 0x74)
NO_SPACE = "ERR Not enough free space"


class NoSpace(RuntimeError):
    """The import does not fit beside the dictionary already on the keyboard."""


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


def parse_dictionary(source):
    return parse_named(source)[0]


class Console:
    def __init__(self, device, serial=None):
        self.device = device
        self.serial = serial
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
        yield Console(device, devices[0]["serial_number"])
    finally:
        device.close()


def read_status(console):
    try:
        status = json.loads(console.command("user_dictionary_status"))
    except ValueError:
        status = None
    if not isinstance(status, dict) or status.get("version") != 3 or status.get("bulk_import") != 2:
        raise RuntimeError("Install a current left firmware build. This one stores the "
                           "personal dictionary in a format this script does not write.")
    if status.get("available") is not True:
        raise RuntimeError("The onboard personal dictionary is unavailable.")
    for key in ("free_segments", "record_segments", "fwd_slots", "rev_slots"):
        if not isinstance(status.get(key), int):
            raise RuntimeError(f"The keyboard's dictionary status has no {key}.")
    return status


def wait_ready(console, timeout=1800):
    """Wait out counting, cleaning and table rebuilds. Returns the settled status."""
    deadline = time.monotonic() + timeout
    waiting = False
    while True:
        status = read_status(console)
        if status.get("state") == "importing":
            # An earlier import was cut off. Drop what it uploaded.
            console.import_command("abort", 30)
        elif status.get("state") == "ready" and isinstance(status.get("entries"), int):
            return status
        if time.monotonic() >= deadline:
            raise TimeoutError("The keyboard is still reorganizing its dictionary. Try again later.")
        if not waiting:
            print("Waiting for the keyboard to finish organizing its dictionary…", flush=True)
            waiting = True
        time.sleep(0.5)


def send_edit(console, text, timeout=60, patience=1800):
    """Send one edit, retrying while the keyboard rebuilds a lookup table."""
    deadline = time.monotonic() + patience
    while True:
        reply = console.command(text, timeout)
        if not reply.startswith("ERR") or "busy" not in reply or time.monotonic() >= deadline:
            return reply
        time.sleep(0.5)


def export(console, status):
    """The keyboard's dictionary as (entries, outline text of each)."""
    entries, names = parse_named(console.command("print_dictionary user_dictionary", 1800))
    if len(entries) != status["entries"]:
        raise RuntimeError("The keyboard returned an incomplete dictionary export. Try again.")
    return entries, names


def write_json(path, entries, names):
    """Write a Plover JSON file. Never replaces an existing file."""
    data = {names[strokes]: text.decode("utf-8") for strokes, text in entries.items()}
    with open(path, "x", encoding="utf-8", newline="\n") as file:
        json.dump(data, file, ensure_ascii=False, indent=0)
        file.write("\n")


def line_command(outline, text):
    """The add_user_entry line for one entry, or None if it cannot be a console line."""
    translation = text.decode("utf-8")
    command = f"add_user_entry {outline} {translation}"
    if (not translation or translation != translation.strip() or
            any(ord(character) < 32 or ord(character) == 127 for character in command) or
            len(command.encode("utf-8")) > LINE_BYTES):
        return None
    return command


def add_entries(console, commands):
    for count, command in enumerate(commands, 1):
        reply = send_edit(console, command)
        if reply != "OK":
            raise RuntimeError(f"{reply} (after {count - 1:,} of {len(commands):,} entries were saved)")
        if count == len(commands) or count % 100 == 0:
            print(f"Saved {count:,}/{len(commands):,} entries", flush=True)


def send_segments(console, segments):
    """Upload built segments in plan order and commit them. Aborts on any failure."""
    total = sum(len(segment) for segment in segments)
    sent = 0
    try:
        for index, segment in enumerate(segments):
            for offset in range(0, len(segment), CHUNK_BYTES):
                chunk = segment[offset:offset + CHUNK_BYTES]
                console.import_command(f"data {index} {offset} {chunk.hex()}")
                before, sent = sent, sent + len(chunk)
                if sent == total or before // (64 * 1024) != sent // (64 * 1024):
                    print(f"Sent {sent:,}/{total:,} bytes", flush=True)
        print("Verifying and committing the dictionary…", flush=True)
        console.import_command("finish", 1800)
    except (Exception, KeyboardInterrupt):
        try:
            console.import_command("abort", 5)
        except Exception:
            pass
        raise


def upload(console, entries, replace=False):
    """Bulk import. Raises NoSpace, with the keyboard unchanged, if `replace` is needed."""
    image = Image(entries)
    request = (f"user_dictionary_import begin {image.record_segments} "
               f"{image.forward_segments} {image.reverse_segments}")
    reply = send_edit(console, request + (" replace" if replace else ""), 120)
    if reply.startswith(NO_SPACE):
        raise NoSpace(reply)
    if reply.startswith("ERR"):
        raise RuntimeError(reply)
    plan = json.loads(reply)
    send_segments(console, image.build(plan["first_sequence"], plan["fwd_epoch"],
                                       plan["rev_epoch"], plan["segments"]))


def capacity_segments(status):
    """Every segment of the region, from the counts the keyboard reports."""
    slots_per_segment = (0x10000 - 16) // 4
    return (status["free_segments"] + status["record_segments"] + 1 +
            (status["fwd_slots"] + status["rev_slots"]) // slots_per_segment)


def bulk_import(console, status, entries, write_backup):
    """Upload `entries` as the whole dictionary.

    The import is atomic when it fits beside the current dictionary. When it
    does not, `write_backup()` saves the merged dictionary on the host first,
    because the keyboard then has to erase its copy to make room.
    """
    needed = Image(entries).total_segments
    room = capacity_segments(status) - OVERHEAD_SEGMENTS
    if needed > room:
        raise ValueError(f"The merged dictionary needs {needed} flash segments. "
                         f"The keyboard can hold {room}. Nothing was changed.")
    try:
        upload(console, entries)
        return
    except NoSpace:
        pass
    backup = write_backup()
    print("There is not enough free space to keep the current dictionary during the import.\n"
          f"The merged dictionary is saved in {backup}.\n"
          "Replacing the keyboard's dictionary. If this is interrupted, run --import with that file.",
          flush=True)
    try:
        upload(console, entries, replace=True)
    except NoSpace:
        raise RuntimeError("The keyboard could not make room. Its personal dictionary is now "
                           f"empty. The complete copy is in {backup}.") from None


def backup_path(path):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return path.with_name(f"{path.stem}.backup-{stamp}.json")


def import_file(path, serial=None):
    entries, names = parse_named(path.read_text(encoding="utf-8-sig"))
    if not entries:
        raise ValueError("The JSON file has no entries.")
    with open_console(serial) as console:
        status = wait_ready(console)
        existing, existing_names = export(console, status)
        changed = {key: value for key, value in entries.items() if existing.get(key) != value}
        if not changed:
            print("All entries already match the keyboard.")
            return
        replacements = sum(key in existing for key in changed)
        merged = dict(existing)
        merged.update(entries)
        print(f"Adding {len(changed) - replacements:,}, replacing {replacements:,}, "
              f"saving {len(merged):,} entries.", flush=True)
        commands = [line_command(names[key], value) for key, value in changed.items()]
        if len(changed) < SMALL_CHANGE and all(commands):
            add_entries(console, commands)
        else:
            def write_backup():
                target = backup_path(path)
                write_json(target, merged, {**existing_names, **names})
                return target

            bulk_import(console, status, merged, write_backup)
        if wait_ready(console)["entries"] != len(merged):
            raise RuntimeError("The keyboard's entry count does not match the import. "
                               "Back up the dictionary and check it.")
        print("Personal dictionary saved and verified.")


def backup_file(path, serial=None):
    """Export the keyboard's personal dictionary to a new Plover JSON file."""
    if path.exists():
        raise ValueError(f"{path} already exists. Choose a new file name for the backup.")
    with open_console(serial) as console:
        entries, names = export(console, wait_ready(console))
    write_json(path, entries, names)
    print(f"Saved {len(entries):,} personal entries to {path}.")
