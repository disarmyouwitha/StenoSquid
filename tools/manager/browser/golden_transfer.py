"""Golden vectors for publish.js (native_update.transfer and publish over the Base64 console path).

The oracle is Python's transfer() against a scripted console that serves a device-shaped flash image. The console has no
HID device, so Python takes the Base64 path; the binary session has its own tests (test_transfer_client.mjs). Each case
records every console command with its timeout and reply, the progress messages, and the result or failure.
"""
import base64
import json
from pathlib import Path
import sys

import golden_decode as images  # also puts the tools/ directories on sys.path
import golden_snapshot as base_vectors
import golden_plan
from build_native_image import SMALL_POOL_END
from native_update import Snapshot, transfer

OUT = Path(__file__).with_name("golden") / "transfer_vectors.json"


class Console:
    serial = "AB12"

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
            entry["d"] = data.rstrip(bytes([255])).hex()
        elif verb == "commit":
            reply = entry["r"] = "OK Rebooting"
        else:
            reply = entry["r"] = "OK"
        if override is not None:
            reply = entry["r"] = override
            entry.pop("d", None)
        self.log.append(entry)
        return reply


def case(title, image, *, incoming=None, script=None, answer=None, **options):
    address, size, crc = base_vectors.catalog_of(image)
    probe = Snapshot(lambda a, n: bytes(image[a:a + n]), address, size, crc, metadata_only=True, pool_end=SMALL_POOL_END)
    answer = answer or f"OK {address} {size} {crc} {base_vectors.ownership(probe, SMALL_POOL_END)}"
    console, messages = Console(image, answer, script), []
    try:
        result = transfer(console, incoming, messages.append, **{key: tuple(value) if isinstance(value, list) else value
                                                                  for key, value in options.items()})
        outcome = {"result": result}
    except Exception as error:
        outcome = {"error": {"type": type(error).__name__, "message": str(error)}}
    record = {"title": title, "answer": answer, "options": options, "script": [[verb, ordinal, reply] for (verb, ordinal), reply in sorted(console.script.items())],
              "incoming": None if incoming is None else [[list(strokes), text.hex()] for strokes, text in incoming.items()],
              "expect": outcome, "progress": messages, "log": console.log}
    return record, outcome


def generate():
    image, _ = images.effective_image()
    cases = []

    def add(title, **kwargs):
        record, outcome = case(title, image, **kwargs)
        cases.append(record)
        return outcome

    add("compact (nothing to reclaim or a journal pass)")
    preview = add("preview cleanup", preview_compact=True)
    token = preview.get("result", {}).get("token")
    add("cleanup with the previewed token", expected_compact=token)
    add("cleanup with a stale token", expected_compact="0" * 64)
    add("cleanup with no token while previewing nothing", expected_compact=None)
    add("preview with a move", preview_compact=True, move=["Custom B", 1])
    add("expected token with a rename", expected_compact="x", rename=["Custom A", "Renamed"])
    add("move a dictionary", move=["Custom B", 1])
    add("move to the same priority", move=["Custom A", 8])
    add("rename a dictionary", rename=["Custom A", "Renamed"])
    add("rename to the same name", rename=["Custom A", "Custom A"])
    add("rename onto an existing name", rename=["Custom A", "Custom B"])
    add("delete a dictionary", delete="Custom B")
    add("delete an unknown dictionary", delete="Nothing")
    add("personal edits already match", incoming={(0x10,): b"hello", (0x30,): "café".encode()})
    add("personal edits, new and changed", incoming={(0x10,): b"changed", (0x2000,): b"new entry", (0x2001, 0x2002): "naïve".encode()})
    add("empty incoming", incoming={})
    add("many new personal entries", incoming={(0x2000 + i,): b"entry %d" % i + b"y" * 900 for i in range(120)})
    add("begin refused", move=["Custom B", 1], script={("begin", 0): "ERR Native store busy"})
    add("erase refused", move=["Custom B", 1], script={("erase", 1): "ERR flash"})
    add("write refused", incoming={(0x2000,): b"new entry"}, script={("write", 1): "ERR flash"})
    add("commit gives another reply", move=["Custom B", 1], script={("commit", 1): "OK"})
    add("commit refused", move=["Custom B", 1], script={("commit", 1): "ERR Native commit failed"})
    add("abort refused after a refusal", move=["Custom B", 1], script={("erase", 1): "ERR flash", ("abort", 0): "ERR no"})
    add("abort refused after a no-op", move=["Custom A", 8], script={("abort", 0): "ERR no"})
    add("flash busy on a read", move=["Custom B", 1], script={("read", 1): "ERR Native flash busy"})
    add("short read", delete="Custom B", script={("read", 1): "OK AAAA"})
    add("malformed begin answer", move=["Custom B", 1], answer="OK 1 2")
    big = golden_plan.big_fixture(SMALL_POOL_END)

    def add_big(title, **kwargs):
        record, outcome = case("two dead journals: " + title, big, **kwargs)
        cases.append(record)
        return outcome

    add_big("compact")
    preview = add_big("preview cleanup", preview_compact=True)
    add_big("cleanup with the previewed token", expected_compact=preview["result"]["token"])
    add_big("cleanup with a stale token", expected_compact="1" * 64)
    add_big("personal edit on several journals", incoming={(0x1000 + 90,): b"changed", (0x5000,): b"new"})
    return {"schema": 1, "cases": cases}


def main():
    text = json.dumps(generate(), indent=0) + chr(10)
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8").replace(chr(13) + chr(10), chr(10)) != text:
            sys.exit("golden/transfer_vectors.json is stale; rerun golden_transfer.py")
        print("transfer vectors are current")
        return
    OUT.write_text(text, encoding="utf-8", newline=chr(10))
    print(f"wrote {OUT} ({len(text) // 1024} KiB, {len(json.loads(text)['cases'])} cases)")


if __name__ == "__main__":
    main()
