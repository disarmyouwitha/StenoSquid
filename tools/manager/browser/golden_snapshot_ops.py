"""Golden vectors for snapshot_ops.js: merge_snapshots, clear_personal, change_snapshot, named_import and factory_package.

The oracle is the Python functions themselves, run against a scripted console that serves a device-shaped flash image (built with
the real compiler, so compact dictionaries decode) and records every command with its timeout and reply. The JS test replays each
log in order and requires the same commands, the same progress text, and the same result or failure.
"""
import base64
import json
from pathlib import Path
import struct
import sys

import golden_decode as images  # also puts the tools/ directories on sys.path
import golden_package
import golden_plan as plan_vectors
import golden_snapshot as base_vectors
import golden_snapshot_plan as snap_vectors
import native_base_update
from build_native_image import SMALL_POOL_END
from compile_dict import parse_outline
from native_base_update import factory_package
from native_import_plan import named_import
from native_snapshot import clear_personal, change_snapshot, compile_snapshot, merge_snapshots, plan_named_snapshot
from native_update import Edit, Snapshot, row_name

OUT = Path(__file__).with_name("golden") / "snapshot_ops_vectors.json"
PREFIX = "native_install update "
CAPABILITIES = {"snapshot-capabilities": "OK SNAPSHOT2", "personal-snapshot-capabilities": "OK PERSONAL1",
                "journal-snapshot-capabilities": "OK JOURNAL1", "named-capabilities": "OK NAMED1 16",
                "snapshot-merge-capabilities": "OK MERGE1 16", "factory-member-capabilities": "OK FACTORY2",
                "repack-capabilities": "OK REPACK1", "recovery-capabilities": "OK RECOVERY1"}


class Console:
    serial = "AB12"

    def __init__(self, image, answer, replies):
        self.image, self.answer, self.replies, self.log, self.counts = image, answer, replies, [], {}

    def command(self, text, timeout=30):
        words = text[len(PREFIX):].split() if text.startswith(PREFIX) else [text]
        verb = words[0]
        ordinal = self.counts[verb] = self.counts.get(verb, 0) + 1
        entry = {"c": text, "t": timeout}
        override = self.replies.get((verb, ordinal), self.replies.get((verb, 0)))
        if override is not None:
            reply = entry["r"] = override
        elif text.startswith(PREFIX + "begin"):
            reply = entry["r"] = self.answer
        elif verb == "read":
            data = bytes(self.image[int(words[1]):int(words[1]) + int(words[2])])
            reply = "OK " + base64.b64encode(data).decode()
            entry["d"] = data.rstrip(bytes([255])).hex()
        elif verb == "commit":
            reply = entry["r"] = "OK Rebooting"
        elif verb in ("erase", "write", "abort"):
            reply = entry["r"] = "OK"
        elif text.startswith(PREFIX) and verb in CAPABILITIES:
            reply = entry["r"] = CAPABILITIES[verb]
        elif text == "user_dictionary_status":
            reply = entry["r"] = json.dumps({"generation": 12, "tail": 3})
        elif text == "native_catalog":
            reply = entry["r"] = self.catalog()
        else:
            raise AssertionError("unscripted command " + text)
        self.log.append(entry)
        return reply

    def catalog(self):
        snapshot = plan_vectors.snapshot_of(self.image, SMALL_POOL_END)
        rows = []
        for row in snapshot.rows:
            identity, name = struct.unpack_from("<I", row)[0], row_name(row)
            kind = "personal" if name == "user_dictionary" else "base" if 2 <= identity <= 7 else "snapshot" if row[11] == 1 else "named"
            rows.append({"id": identity, "name": name, "kind": kind})
        return json.dumps(rows)


def answer_for(image, owned=None):
    address, size, crc = base_vectors.catalog_of(image)
    probe = Snapshot(lambda a, n: bytes(image[a:a + n]), address, size, crc, metadata_only=True, pool_end=SMALL_POOL_END)
    bitmap = "f" * 64 if owned == "all" else base_vectors.ownership(probe, SMALL_POOL_END)
    return f"OK {address} {size} {crc} {bitmap}"


def build_images():
    base, _ = images.effective_image()
    image = bytearray(base)
    for number, (title, entries) in enumerate((
            ("Snap One", {"KAT": b"cat", "TKOG": b"dog", "KAT/TKOG": b"cat dog", "STKPWHR": b"shared one"}),
            ("Snap Two", {"STKPWHR": b"shared two", "-T": b"the", "PWOPB": b"ball"})), 1):
        snapshot = plan_vectors.snapshot_of(image, SMALL_POOL_END)
        compiled = compile_snapshot(title, {parse_outline(key): value for key, value in entries.items()})
        update = plan_named_snapshot(snapshot, plan_vectors.owned(image, SMALL_POOL_END), title, compiled, limit=16)
        image = snap_vectors.apply_update(image, update, 10 * number)
    snapshot = plan_vectors.snapshot_of(image, SMALL_POOL_END)
    ids = {row_name(row): struct.unpack_from("<I", row)[0] for row in snapshot.rows}
    # Journal edits on both compact dictionaries: a changed entry, a removed entry and a new one.
    image = bytearray(image)
    base_vectors.append_tail(image, SMALL_POOL_END, [
        Edit(0, ids["Snap One"], 0, tuple(parse_outline("KAT")), b"cat (edited)"),
        Edit(0, ids["Snap One"], 1, tuple(parse_outline("TKOG")), b""),
        Edit(0, ids["Snap Two"], 0, tuple(parse_outline("PWOPB/KAT")), b"new in two")])
    return bytes(base), bytes(image), ids


def without_package(source, key):
    """The base image after the factory package `key` was uninstalled (so a restore has something to do)."""
    snapshot = plan_vectors.snapshot_of(bytearray(source), SMALL_POOL_END)
    update, _ = native_base_update.plan_package(snapshot, plan_vectors.owned(source, SMALL_POOL_END), key)
    return bytes(snap_vectors.apply_update(source, update, 30))


def run(function, console, progress):
    try:
        return {"result": function(console, progress)}
    except Exception as error:
        return {"error": {"type": type(error).__name__, "message": str(error)}}


def generate():
    base, image, ids = build_images()
    pictures = {"base": base, "snaps": image, "no-numbers": without_package(base, "lapwing-numbers"), "no-plover": without_package(base, "plover")}
    # adjacent compact rows: the first imported ends up below the second
    cases = []
    upper, lower = ids["Snap Two"], ids["Snap One"]

    def add(title, source, call, args, *, owned=None, replies=None, prepare=None):
        label = next(name for name, data in pictures.items() if data is source)
        console = Console(source, answer_for(source, owned), replies or {})
        messages = []
        if prepare is not None:
            native_base_update.prepare = lambda name, spec=prepare: golden_package.build_compiled(spec, name)
        outcome = run(lambda c, p: call(c, p), console, messages.append)
        cases.append({"title": title, "fn": call.__name__, "image": label, "args": args, "owned": owned,
                      "replies": [[verb, ordinal, reply] for (verb, ordinal), reply in sorted((replies or {}).items())],
                      "prepare": prepare, "expect": outcome, "progress": messages, "log": console.log})
        return outcome

    def merge(title, up, low, expected=None, **kw):
        def merge_snapshots_call(console, progress):
            return merge_snapshots(console, up, low, expected=expected, progress=progress)
        merge_snapshots_call.__name__ = "mergeSnapshots"
        return add(title, image, merge_snapshots_call, {"upper": up, "lower": low, "expected": expected}, **kw)

    preview = merge("preview a merge", upper, lower)
    token = preview["result"].get("token")
    merge("apply the previewed merge", upper, lower, token)
    merge("apply with a stale token", upper, lower, "0" * 64)
    merge("preview with no staging space", upper, lower, owned="all")
    merge("apply with no staging space", upper, lower, "0" * 64, owned="all")
    merge("preview with no space and no repack support", upper, lower, owned="all",
          replies={("repack-capabilities", 0): "ERR no"})
    merge("not adjacent", lower, upper)
    merge("not integers", "a", lower)
    merge("identity zero", 0, lower)
    merge("merging unavailable", upper, lower, replies={("snapshot-merge-capabilities", 0): "OK MERGE1 3"})
    merge("begin refused", upper, lower, replies={("begin", 0): "ERR Native store busy"})
    merge("flash busy on a read", upper, lower, replies={("read", 40): "ERR Native flash busy"})

    def clear(title, **kw):
        def clear_call(console, progress):
            return clear_personal(console, 1, progress)
        clear_call.__name__ = "clearPersonal"
        return add(title, image, clear_call, {"identity": 1}, **kw)

    clear("clear the personal dictionary")
    clear("clear: begin refused", replies={("begin", 0): "ERR Native store busy"})
    clear("clear: commit refused", replies={("commit", 1): "ERR bad"})
    clear("clear: no staging space", owned="all")

    def change_with(title, operation, name, source, replies=None, owned=None, **options):
        def change_call(console, progress):
            return change_snapshot(console, operation, name, progress=progress, **options)
        change_call.__name__ = "changeSnapshot"
        return add(title, source, change_call, {"operation": operation, "name": name, **options}, replies=replies, owned=owned)

    for operation, name, source, label in (("personal", "user_dictionary", base, "personal journal"), ("convert", "Custom A", base, "named journal"),
                                           ("consolidate", "Snap One", image, "snapshot")):
        preview = change_with(f"preview {label}", operation, name, source, preview=True)
        token = preview.get("result", {}).get("token") if isinstance(preview.get("result"), dict) else None
        change_with(f"stage {label} with its token", operation, name, source, stage=True, expected=token)
        change_with(f"stage {label} with a stale token", operation, name, source, stage=True, expected="1" * 64)
        change_with(f"stage {label} with no preview", operation, name, source, stage=True)
        change_with(f"preview {label} with target id", operation, name, source, preview=True, target_id=1)
        change_with(f"preview {label}, no staging space", operation, name, source, preview=True, owned="all")
    change_with("delete a snapshot dictionary", "delete", "Snap One", image, stage=True)
    change_with("delete with a preview flag", "delete", "Snap One", image, preview=True)
    change_with("preview and stage together", "consolidate", "Snap One", image, preview=True, stage=True)
    change_with("unknown operation", "explode", "Snap One", image)
    change_with("wrong firmware", "consolidate", "Snap One", image, replies={("snapshot-capabilities", 0): "OK SNAPSHOT1"})
    change_with("personal firmware missing", "personal", "user_dictionary", base, replies={("personal-snapshot-capabilities", 0): "OK"})
    change_with("journal firmware missing", "convert", "Custom A", base, replies={("journal-snapshot-capabilities", 0): "OK"})
    change_with("dictionary not eligible", "consolidate", "Custom A", base)
    change_with("invalid name", "consolidate", "bad name ", image)
    change_with("catalog changed before staging", "consolidate", "Snap One", image, preview=True,
                replies={("native_catalog", 0): json.dumps([{"id": 99, "name": "Snap One", "kind": "snapshot"}])})
    change_with("convert an empty journal (preview)", "convert", "Custom B", base, preview=True)

    def named(title, name, entries, source=base, replies=None, owned=None, **options):
        def named_call(console, progress):
            return named_import(console, name, {tuple(parse_outline(key)): text for key, text in entries}, progress=progress, **options)
        named_call.__name__ = "namedImport"
        return add(title, source, named_call, {"name": name, "entries": [[key, text.hex()] for key, text in entries], **options},
                   replies=replies, owned=owned)

    words = [("KAT", b"cat"), ("TKOG", b"dog"), ("KAT/TKOG", b"cat dog"), ("PWOPB", "balle".encode())]
    preview = named("preview a named import", "My Names", words)
    token = preview["result"].get("token")
    named("stage with the previewed token", "My Names", words, stage=True, expected=token)
    named("stage with a stale token", "My Names", words, stage=True, expected="2" * 64)
    named("stage without a token", "My Names", words, stage=True)
    named("duplicate name", "Custom A", words)
    named("invalid name", "bad name ", words)
    named("zero entries", "Empty", [])
    named("no staging space (preview)", "My Names", words, owned="all")
    named("no staging space (stage)", "My Names", words, owned="all", stage=True)
    named("firmware without compact imports", "My Names", words, replies={("snapshot-capabilities", 0): "OK SNAPSHOT1"})
    named("firmware without named imports", "My Names", words, replies={("named-capabilities", 0): "OK NAMED2 16"})
    named("malformed named capability", "My Names", words, replies={("named-capabilities", 0): "OK NAMED1 x"})
    named("status without generation", "My Names", words, replies={("user_dictionary_status", 0): json.dumps({"tail": 3})})
    named("status with a tail out of range", "My Names", words, replies={("user_dictionary_status", 0): json.dumps({"generation": 1, "tail": 300})})
    named("dictionary limit", "My Names", words, replies={("named-capabilities", 0): "OK NAMED1 3"})
    named("on the snapshot catalog", "Third", words, source=image)

    def package(title, key, restore=False, confirmation=None, replies=None, owned=None, prepare=None, source=base):
        def package_call(console, progress):
            return factory_package(console, key, restore=restore, confirmation=confirmation, progress=progress)
        package_call.__name__ = "factoryPackage"
        return add(title, source, package_call, {"package": key, "restore": restore, "confirmation": confirmation}, replies=replies,
                   owned=owned, prepare=prepare)

    spec = {"length": 40000, "pointers": [0, 8, 4096], "pointerDelta": None, "digest": "ab" * 32}
    for key, gone in (("lapwing-numbers", "no-numbers"), ("plover", "no-plover")):
        preview = package(f"preview uninstalling {key}", key)
        approval = preview.get("result", {}).get("confirmation") if isinstance(preview.get("result"), dict) else None
        package(f"apply uninstalling {key}", key, confirmation=approval)
        package(f"apply uninstalling {key} with a stale approval", key, confirmation="3" * 64)
        preview = package(f"preview restoring {key}", key, restore=True, prepare=spec, source=pictures[gone])
        approval = preview.get("result", {}).get("confirmation") if isinstance(preview.get("result"), dict) else None
        package(f"apply restoring {key}", key, restore=True, confirmation=approval, prepare=spec, source=pictures[gone])
        package(f"restore {key} that is still installed", key, restore=True, prepare=spec)
    package("unknown package", "nothing")
    package("old firmware", "plover", replies={("factory-member-capabilities", 0): "OK FACTORY1"})
    package("begin refused", "plover", replies={("begin", 0): "ERR Native store busy"})
    package("apply with no space", "plover", restore=True, prepare=spec, owned="all", confirmation="4" * 64, source=pictures["no-plover"])
    package("restore with a bad pointer", "plover", restore=True, prepare={**spec, "pointerDelta": 5}, source=pictures["no-plover"])
    return {"schema": 1, "cases": cases}


def main():
    text = json.dumps(generate(), indent=0) + chr(10)
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8").replace(chr(13) + chr(10), chr(10)) != text:
            sys.exit("golden/snapshot_ops_vectors.json is stale; rerun golden_snapshot_ops.py")
        print("snapshot operation vectors are current")
        return
    OUT.write_text(text, encoding="utf-8", newline=chr(10))
    print(f"wrote {OUT} ({len(text) // 1024} KiB, {len(json.loads(text)['cases'])} cases)")


if __name__ == "__main__":
    main()
