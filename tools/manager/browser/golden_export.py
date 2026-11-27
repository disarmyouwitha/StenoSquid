"""Golden vectors for export.js (native_update.export_dictionary through a console).

The oracle is Python's export_dictionary against a scripted console that serves a device-shaped flash image. Each case
records the full console transcript (every command and its reply, in order), the entries or the failure, the bytes
reported through on_read, and whether on_snapshot was called.
"""
import base64
import json
from pathlib import Path
import sys

import golden_decode as images  # also puts the tools/ directories on sys.path
import golden_snapshot as base_vectors
from build_native_image import SEGMENT, SMALL_POOL_END
from native_update import export_dictionary, Snapshot

OUT = Path(__file__).with_name("golden") / "export_vectors.json"


class Console:
    """Answers native_install update begin/read/abort from an image; `script` overrides replies by (verb, ordinal)."""

    def __init__(self, image, answer, script=None):
        self.image, self.answer, self.script, self.log, self.counts = image, answer, script or {}, [], {}

    def command(self, text, timeout=30):
        prefix = "native_install update "
        assert text.startswith(prefix), text
        words = text[len(prefix):].split()
        verb = words[0]
        ordinal = self.counts[verb] = self.counts.get(verb, 0) + 1
        override = self.script.get((verb, ordinal), self.script.get((verb, 0)))
        entry = {"c": text, "t": timeout}
        if verb == "begin":
            reply = entry["r"] = self.answer
        elif verb == "read":
            data = bytes(self.image[int(words[1]):int(words[1]) + int(words[2])])
            reply = "OK " + base64.b64encode(data).decode()
            entry["d"] = data.rstrip(bytes([255])).hex()  # the flash bytes served (trailing erased bytes are implied)
        else:
            reply = entry["r"] = "OK"
        if override is not None:
            reply = entry["r"] = override
            entry.pop("d", None)
        self.log.append(entry)
        return reply


def case(title, image, name, *, answer=None, script=None, trim=False, snapshot_hook=False):
    address, size, crc = base_vectors.catalog_of(image)
    probe = Snapshot(lambda a, n: bytes(image[a:a + n]), address, size, crc, metadata_only=True, pool_end=SMALL_POOL_END)
    answer = answer or f"OK {address} {size} {crc} {base_vectors.ownership(probe, SMALL_POOL_END)}"
    console = Console(image, answer, script)
    counted, hooked = [], []
    try:
        entries = export_dictionary(console, name, on_read=counted.append,
                                    on_snapshot=hooked.append if snapshot_hook else None, trim_journals=trim)
        result = {"entries": [list(pair) for pair in entries.items()]}
    except Exception as error:
        result = {"error": {"type": type(error).__name__, "message": str(error)}}
    return {"title": title, "name": name, "answer": answer, "trim": trim, "snapshot_hook": snapshot_hook, "script": [
        [verb, ordinal, reply] for (verb, ordinal), reply in sorted(console.script.items())],
        "expect": result, "bytes": sum(counted), "hooked": len(hooked), "log": console.log}


def generate():
    image, rows = images.effective_image()
    cases = []
    for _, name, _ in rows:
        for trim in (False, True):
            cases.append(case(f"export {name}, trim={trim}", image, name, trim=trim, snapshot_hook=trim))
    cases.append(case("unknown dictionary", image, "Nothing"))
    cases.append(case("begin refused", image, "Custom A", script={("begin", 0): "ERR Native store busy"}))
    cases.append(case("begin refused, abort still sent", image, "Custom A", script={("begin", 0): "ERR Native flash busy"}))
    cases.append(case("abort refused after success", image, "Custom A", script={("abort", 0): "ERR no"}))
    cases.append(case("abort refused after failure", image, "Nothing", script={("abort", 0): "ERR no"}))
    cases.append(case("flash busy on the first read", image, "Custom A", script={("read", 1): "ERR Native flash busy"}))
    cases.append(case("flash busy on a later read", image, "Lapwing Commands", script={("read", 5): "ERR Native flash busy"}))
    cases.append(case("short read", image, "Custom A", script={("read", 3): "OK AAAA"}))
    cases.append(case("bad base64", image, "Custom A", script={("read", 3): "OK !!!!"}))
    cases.append(case("malformed answer", image, "Custom A", answer="OK 1 2"))
    cases.append(case("bad ownership length", image, "Custom A", answer="OK 65568 100 1 abcd"))
    address, size, crc = base_vectors.catalog_of(image)
    cases.append(case("wrong catalog checksum", image, "Custom A", answer=f"OK {address} {size} {crc ^ 1} " + "0" * 64))
    damaged = bytearray(image)
    probe = Snapshot(lambda a, n: bytes(image[a:a + n]), address, size, crc, metadata_only=True, pool_end=SMALL_POOL_END)
    damaged[probe.extents[0][0] * SEGMENT + 20] ^= 1
    cases.append(case("base image fails its checksum", damaged, "Lapwing Commands"))
    return {"schema": 1, "cases": cases}


def main():
    text = json.dumps(generate(), indent=0) + chr(10)
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8").replace(chr(13) + chr(10), chr(10)) != text:
            sys.exit("golden/export_vectors.json is stale; rerun golden_export.py")
        print("export vectors are current")
        return
    OUT.write_text(text, encoding="utf-8", newline=chr(10))
    print(f"wrote {OUT} ({len(text) // 1024} KiB, {len(json.loads(text)['cases'])} cases)")


if __name__ == "__main__":
    main()
