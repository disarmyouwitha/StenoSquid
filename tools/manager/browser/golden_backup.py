"""Golden vectors for backup.js (native_update.backup_all through a console).

The oracle is Python's backup_all against a scripted console serving a device-shaped flash image. Each case records the whole
console transcript, the progress calls, and the backup (or failure). The capture timestamp is the one thing that differs between
runs, so its format is checked and it is replaced by a fixed value (with the checksum recomputed).
"""
import base64
import json
from pathlib import Path
import re
import sys

import golden_decode as images  # also puts the tools/ directories on sys.path
import golden_snapshot as base_vectors
import native_recovery
from native_update import Snapshot, backup_all

OUT = Path(__file__).with_name("golden") / "backup_vectors.json"
FIXED = "2026-11-26T12:00:00.123456+00:00"
SHAPE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{6})?\+00:00$")
READY = "NATIVE2 READY 42"


class Console:
    def __init__(self, image, answer, layouts, status, serial, script):
        self.image, self.answer, self.layouts, self.status, self.serial = image, answer, list(layouts), status, serial
        self.script, self.log, self.counts = script, [], {}

    def command(self, text, timeout=30):
        entry = {"c": text, "t": timeout}
        if text == "get_parameter host_layout":
            reply = entry["r"] = self.layouts.pop(0) if len(self.layouts) > 1 else self.layouts[0]
        elif text == "native_install status":
            reply = entry["r"] = self.status
        else:
            words = text.replace("native_install update ", "", 1).split()
            verb = words[0]
            ordinal = self.counts[verb] = self.counts.get(verb, 0) + 1
            if verb == "begin":
                reply = entry["r"] = self.answer
            elif verb == "read":
                data = bytes(self.image[int(words[1]):int(words[1]) + int(words[2])])
                reply = "OK " + base64.b64encode(data).decode()
                entry["d"] = data.rstrip(bytes([255])).hex()
            else:
                reply = entry["r"] = "OK"
            override = self.script.get((verb, ordinal), self.script.get((verb, 0)))
            if override is not None:
                reply = entry["r"] = override
                entry.pop("d", None)
        self.log.append(entry)
        return reply


def case(title, image, *, layouts=("Mac U.S.",), status=READY, serial="AB12", script=None, progress=True, answer=None):
    address, size, crc = base_vectors.catalog_of(image)
    probe = Snapshot(lambda a, n: bytes(image[a:a + n]), address, size, crc, metadata_only=True, pool_end=255)
    answer = answer or f"OK {address} {size} {crc} {base_vectors.ownership(probe, 255)}"
    console = Console(image, answer, layouts, status, serial, script or {})
    calls = []
    try:
        result = backup_all(console, on_progress=(lambda received, total: calls.append([received, total])) if progress else None)
        if "capture" in result:
            assert SHAPE.match(result["capture"]["timestamp"]), result["capture"]["timestamp"]
            result["capture"]["timestamp"] = FIXED
            result["sha256"] = native_recovery.digest(result)
        outcome = {"backup": result}
    except Exception as error:
        outcome = {"error": {"type": type(error).__name__, "message": str(error)}}
    return {"title": title, "serial": serial, "progress": progress, "calls": calls, "expect": outcome, "log": console.log}


def generate():
    image, _ = images.effective_image()
    cases = [
        case("a backup of a keyboard with a serial", image),
        case("a backup without progress reporting", image, progress=False),
        case("a backup of a console without a serial", image, serial=None),
        case("a backup of a console with an empty serial", image, serial=""),
        case("the other host layout", image, layouts=("Windows U.S. (plain text)",)),
        case("an unsupported host layout", image, layouts=("Linux",)),
        case("host layout changed during the backup", image, layouts=("Mac U.S.", "Windows U.S. (plain text)")),
        case("host layout became empty", image, layouts=("Mac U.S.", "")),
        case("begin refused", image, script={("begin", 0): "ERR Native store busy"}),
        case("abort refused after success", image, script={("abort", 0): "ERR no"}),
        case("abort refused after failure", image, layouts=("Linux",), script={("abort", 0): "ERR no"}),
        case("flash busy on a read", image, script={("read", 4): "ERR Native flash busy"}),
        case("short read", image, script={("read", 4): "OK AAAA"}),
        case("generation 0", image, status="NATIVE2 READY 0"),
        case("a large generation", image, status="NATIVE2 READY 4000000000"),
        case("leading zeros in the generation", image, status="NATIVE2 READY 0042"),
        case("status with extra whitespace", image, status="  NATIVE2   READY\t42 "),
        case("status with only two words", image, status="NATIVE2 READY"),
        case("status with four words", image, status="NATIVE2 READY 42 7"),
        case("status not ready", image, status="NATIVE2 BUSY 42"),
        case("status with another protocol", image, status="NATIVE1 READY 42"),
        case("status generation is not digits", image, status="NATIVE2 READY x"),
        case("status generation is negative", image, status="NATIVE2 READY -1"),
        case("status is an error", image, status="ERR Native store busy"),
        case("status is empty", image, status=""),
        case("a malformed begin answer", image, answer="OK 1 2"),
    ]
    return {"schema": 1, "fixed": FIXED, "cases": cases}


def main():
    text = json.dumps(generate(), indent=0) + chr(10)
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8").replace(chr(13) + chr(10), chr(10)) != text:
            sys.exit("golden/backup_vectors.json is stale; rerun golden_backup.py")
        print("backup vectors are current")
        return
    OUT.write_text(text, encoding="utf-8", newline=chr(10))
    print(f"wrote {OUT} ({len(text) // 1024} KiB, {len(json.loads(text)['cases'])} cases)")


if __name__ == "__main__":
    main()
