"""Golden vectors for editor.js (manager_editor.read_document / change_document / clear_documents).

The oracle is Python. Each scenario is a short session on one scripted keyboard (a device-shaped flash image served through the
update console, a scripted status, "OK" for everything else): reads, previews, saves and cache clears in sequence, because the
editor keeps a document cache between calls. One ordered event stream per scenario records every console command with its
timeout and reply, every progress message, every bundled-factory-JSON lookup (a recording fake) and the result or failure of
each step, so the JavaScript must do the same things in the same order. Requests refer to the latest version and preview token
with {"$last": "version" | "token"} because those come from the keyboard.
"""
import base64
import json
from pathlib import Path
import sys

import golden_decode as images  # also puts the tools/ directories on sys.path
import golden_snapshot as base_vectors

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import manager_editor
from native_update import Snapshot, row_name

OUT = Path(__file__).with_name("golden") / "editor_vectors.json"
STATUS = {"ready": True, "read_only": False, "generation": 12, "tail": 5}
BASELINE = [["KAT", "cat"], ["TKOG", "dog"], ["PWAOEUT", "bite"], ["TK-PL", "dumb"]]


def err(error):
    return {"type": type(error).__name__, "message": str(error)}


class World:
    def __init__(self, image, answer, *, serial="AB12", statuses=None, overrides=None):
        self.image, self.answer, self.serial = image, answer, serial
        self.statuses = [json.dumps(s) if not isinstance(s, str) else s for s in (statuses or [STATUS])]
        self.overrides = {key: list(value) for key, value in (overrides or {}).items()}
        self.events = []
        self.used = {}

    def command(self, text, timeout=30):
        event = {"e": "cmd", "c": text, "t": timeout}
        self.events.append(event)
        if text == "user_dictionary_status":
            index = self.used["status"] = self.used.get("status", 0) + 1
            reply = self.statuses[min(index, len(self.statuses)) - 1]
        elif text.startswith("native_install update "):
            words = text.replace("native_install update ", "", 1).split()
            if words[0] == "begin":
                reply = self.answer
            elif words[0] == "read":
                data = bytes(self.image[int(words[1]):int(words[1]) + int(words[2])])
                event["d"] = data.rstrip(bytes([255])).hex()
                reply = "OK " + base64.b64encode(data).decode()
            elif words[0] == "snapshot-reset-capabilities":
                reply = "OK RESET1"
            else:
                reply = "OK"
        else:
            reply = "OK"
        for prefix, queue in self.overrides.items():
            if text.startswith(prefix) and queue:
                reply = queue.pop(0)
                event.pop("d", None)
                break
        if "d" not in event:
            event["r"] = reply
        return reply


def make_world(patch=None, **options):
    image, rows = images.effective_image()
    if patch:  # catalog bytes to change (offset in the payload -> value), with the catalog checksum repaired
        import struct
        import zlib
        address, size, _ = base_vectors.catalog_of(image)
        payload = bytearray(image[address:address + size])
        for offset, value in patch.items():
            payload[offset] = value
        image[address:address + size] = payload
        struct.pack_into("<I", image, 65536 + 20, zlib.crc32(bytes(payload)))
    address, size, crc = base_vectors.catalog_of(image)
    probe = Snapshot(lambda a, n: bytes(image[a:a + n]), address, size, crc, metadata_only=True, pool_end=255)
    answer = f"OK {address} {size} {crc} {base_vectors.ownership(probe, 255)}"
    return World(image, answer, **options), probe


def selections():
    _, probe = make_world()
    chosen = {}
    for row in probe.rows:
        identity = int.from_bytes(row[:4], "little")
        name = row_name(row)
        kind = "personal" if identity == 1 else "base" if 2 <= identity <= 7 else "snapshot" if name == "Custom B" else "named"
        chosen[name] = {"id": identity, "name": name, "kind": kind, "source_sha256": row[64:96].hex() or None,
                        "image_crc": 1000 + identity, "image_bytes": 2000 + identity}
    return chosen


def resolve(value, last):
    if isinstance(value, dict) and list(value) == ["$last"]:
        return last[value["$last"]]
    if isinstance(value, dict):
        return {key: resolve(item, last) for key, item in value.items()}
    return value


def scenario(title, steps, *, factory=None, **options):
    """Run `steps` against Python. A step is ("read", name, {force, mode}), ("change", name, request, apply), ("clear", serial)."""
    world, _ = make_world(**{key: value for key, value in options.items() if key != "serial"} | {"serial": options.get("serial", "AB12")})
    chosen = selections()
    manager_editor._documents.clear()
    events, last, results = world.events, {}, []

    def fake_factory(identity, name, digest):
        value = (factory or {}).get(name)
        events.append({"e": "factory", "a": [identity, name, digest], "ret": value})
        return None if value is None else dict(value)

    original = manager_editor.factory_source
    manager_editor.factory_source = fake_factory
    class Console:
        serial = options.get("serial", "AB12")
        command = staticmethod(world.command)

    console = Console()

    def progress(message):
        events.append({"e": "progress", "m": message})

    try:
        for step in steps:
            kind = step[0]
            try:
                if kind == "read":
                    selected = dict(chosen[step[1]])
                    selected.update(step[3] if len(step) > 3 else {})
                    document = manager_editor.read_document(console, selected, progress, **step[2])
                    last.update(version=document["version"], source=document["source"])
                    value = document
                elif kind == "change":
                    selected = dict(chosen[step[1]])
                    selected.update(step[4] if len(step) > 4 else {})
                    request = resolve(step[2], last)
                    value = manager_editor.change_document(console, selected, request, progress, apply=step[3])
                    if not step[3]:
                        last["token"] = value["token"]
                    else:
                        last.update(version=value["version"], source=value["source"])
                elif kind == "setstatus":
                    world.statuses = [json.dumps(s) if not isinstance(s, str) else s for s in step[1]]
                    world.used["status"] = 0
                    value = None
                elif kind == "clear":
                    manager_editor.clear_documents(step[1])
                    value = None
                else:
                    raise ValueError(kind)
                results.append({"ok": value})
            except Exception as error:
                results.append({"error": err(error)})
            events.append({"e": "step", "r": results[-1]})
    finally:
        manager_editor.factory_source = original
        manager_editor._documents.clear()
    return {"title": title, "serial": options.get("serial", "AB12"), "steps": json.loads(json.dumps(steps)), "factory": factory,
            "statuses": options.get("statuses"), "overrides": options.get("overrides"), "events": events}


ROW = 32  # rows start after the 32-byte catalog header; each is 96 bytes


def row_offset(identity, field):
    return ROW + (identity - 1) * 96 + field


def dry_source(name, mode="all", factory=None):
    """The document text the editor shows for `name`, to build realistic edit requests."""
    case = scenario("dry", [("read", name, {"mode": mode})], factory=factory)
    return case["events"][-1]["r"]["ok"]["source"]


def edit(source, add=None, drop=(), change=None):
    data = json.loads(source)
    for key in drop:
        data.pop(key)
    for key, value in (change or {}).items():
        data[key] = value
    data.update(add or {})
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def generate():
    cases = []
    names = list(selections())
    base_name = "Lapwing Base"
    factory = {base_name: BASELINE}

    def add(title, steps, **options):
        cases.append(scenario(title, steps, **options))

    for name in names:
        for mode in ("all", "edits"):
            add(f"read {name}, mode {mode}", [("read", name, {"mode": mode})])
            add(f"read {name}, mode {mode}, with bundled factory JSON", [("read", name, {"mode": mode})], factory={name: BASELINE})
    add("read with a bad mode", [("read", "Custom A", {"mode": "x"})])
    add("read with mode None", [("read", "Custom A", {"mode": None})])
    add("read twice is served from the cache", [("read", "Custom A", {}), ("read", "Custom A", {})])
    add("read twice with force", [("read", "Custom A", {}), ("read", "Custom A", {"force": True})])
    add("the other mode comes from the cache too", [("read", "Custom A", {"mode": "edits"}), ("read", "Custom A", {"mode": "all"})])
    add("the other mode after the whole dictionary", [("read", "Custom A", {"mode": "all"}), ("read", "Custom A", {"mode": "edits"})])
    add("factory: the other mode comes from the cache", [("read", base_name, {"mode": "edits"}), ("read", base_name, {"mode": "all"})], factory=factory)
    add("factory: whole then edits", [("read", base_name, {"mode": "all"}), ("read", base_name, {"mode": "edits"})], factory=factory)
    add("cleared cache", [("read", "Custom A", {}), ("clear", "AB12"), ("read", "Custom A", {})])
    add("clearing another serial keeps the cache", [("read", "Custom A", {}), ("clear", "ZZ"), ("read", "Custom A", {})])
    add("a different serial has its own cache", [("read", "Custom A", {})], serial="CD34")
    add("cache is keyed by the dictionary's identity", [("read", "Custom A", {}), ("read", "Custom A", {}, {"image_crc": 5})])
    add("cache is keyed by the dictionary's size", [("read", "Custom A", {}), ("read", "Custom A", {}, {"image_bytes": 5})])
    add("cache is keyed by the source hash", [("read", "Custom A", {}), ("read", "Custom A", {}, {"source_sha256": "ab"})])
    add("cache is keyed by the generation", [("read", "Custom A", {}), ("read", "Custom A", {})],
        statuses=[STATUS, STATUS, {**STATUS, "generation": 13}, {**STATUS, "generation": 13}])
    add("cache is keyed by the tail", [("read", "Custom A", {}), ("read", "Custom A", {})],
        statuses=[STATUS, STATUS, {**STATUS, "tail": 6}, {**STATUS, "tail": 6}])
    add("cache is not used when the store is not ready", [("read", "Custom A", {}), ("read", "Custom A", {})], statuses=[{**STATUS, "ready": False}])
    add("cache is not used for a boolean generation", [("read", "Custom A", {}), ("read", "Custom A", {})], statuses=[{**STATUS, "generation": True}])
    add("cache is not used for a text generation", [("read", "Custom A", {}), ("read", "Custom A", {})], statuses=[{**STATUS, "generation": "12"}])
    add("cache is not used for a missing tail", [("read", "Custom A", {}), ("read", "Custom A", {})], statuses=[{"ready": True, "generation": 12}])
    add("the dictionary changes while loading (generation)", [("read", "Custom A", {})], statuses=[STATUS, {**STATUS, "generation": 99}])
    add("the dictionary changes while loading (tail)", [("read", "Custom A", {})], statuses=[STATUS, {**STATUS, "tail": 99}])
    add("an unrelated status field changes while loading", [("read", "Custom A", {})], statuses=[STATUS, {**STATUS, "read_only": True}])
    add("the status loses its generation while loading", [("read", "Custom A", {})], statuses=[STATUS, {"ready": True}])
    for odd in ("null", "[]", "5", '"x"', "{}", "garbage", ""):
        add(f"status is {odd or 'empty'}", [("read", "Custom A", {})], statuses=[odd])
    add("status after loading is not an object", [("read", "Custom A", {})], statuses=[STATUS, "[]"])
    add("six dictionaries fill the cache", [("read", n, {}) for n in names[:7]] + [("read", names[0], {})])
    add("cache eviction keeps order of first insertion", [("read", "Custom A", {}), ("read", "Custom B", {}), ("read", "Custom A", {"force": True}), ("read", "Custom B", {})])
    add("unknown dictionary", [("read", "Custom A", {}, {"name": "Nothing", "id": 99})])
    add("another dictionary with the same name", [("read", "Custom A", {}, {"id": 8})])
    add("the factory dictionary changed", [("read", base_name, {"mode": "all"}, {"source_sha256": "00" * 32})], factory=factory)
    add("factory with no source hash", [("read", base_name, {"mode": "all"}, {"source_sha256": None})], factory=factory)
    add("factory: not a base kind", [("read", base_name, {"mode": "all"}, {"kind": "named"})], factory=factory)
    add("a flash error while reading", [("read", "Custom A", {})], overrides={"native_install update read": ["OK", "ERR Native flash busy"]})
    add("begin refused", [("read", "Custom A", {})], overrides={"native_install update begin": ["ERR Native store busy"]})
    add("abort refused", [("read", "Custom A", {})], overrides={"native_install update abort": ["ERR no"]})

    # edits: previews and saves
    sources = {mode: dry_source("Custom A", mode) for mode in ("all", "edits")}
    base_all = dry_source(base_name, "all", factory)
    base_edits = dry_source(base_name, "edits", factory)
    cust_b_edits = dry_source("Custom B", "edits")
    request = lambda source, mode="all", **extra: {"source": source, "version": {"$last": "version"}, "mode": mode, **extra}
    for mode in ("all", "edits"):
        src = sources[mode]
        add(f"preview with no changes, {mode}", [("read", "Custom A", {"mode": mode}), ("change", "Custom A", request(src, mode), False)])
        add(f"preview an added entry, {mode}", [("read", "Custom A", {"mode": mode}),
            ("change", "Custom A", request(edit(src, add={"STKPW": "added"}), mode), False)])
        add(f"preview a changed entry, {mode}", [("read", "Custom A", {"mode": mode}),
            ("change", "Custom A", request(edit(src, change={"A*PB": "different"} if mode == "all" else {"A": "x"}), mode), False)])
    add("preview a removal, all", [("read", "Custom A", {}), ("change", "Custom A", request(edit(sources["all"], drop=["A*PB"]) if "A*PB" in sources["all"] else "{}"), False)])
    add("preview deleting everything", [("read", "Custom A", {}), ("change", "Custom A", request("{}"), False)])
    add("preview deleting everything, edits", [("read", "Custom A", {"mode": "edits"}), ("change", "Custom A", request("{}", "edits"), False)])
    add("preview without reading first", [("change", "Custom A", {"source": sources["all"], "version": "x"}, False)])
    add("preview with a stale version", [("read", "Custom A", {}), ("change", "Custom A", {"source": sources["all"], "version": "stale"}, False)])
    add("preview with no version", [("read", "Custom A", {}), ("change", "Custom A", {"source": sources["all"]}, False)])
    add("preview with a bad mode", [("change", "Custom A", {"source": "{}", "mode": "x"}, False)])
    for bad in (None, 5, [], {}):
        add(f"preview with source {bad!r}", [("change", "Custom A", {"source": bad}, False)])
        add(f"preview edits with source {bad!r}", [("change", "Custom A", {"source": bad, "mode": "edits"}, False)])
    add("preview with no source", [("change", "Custom A", {}, False)])
    for source in ("[]", "5", "null", '"x"', "{", '{"KAT": }', '{"KAT": "a",}', '{"KAT": "a", "KAT": "b"}', '{"KAT": 5}', '{"KAT": null}',
                   '{"KAT": ["x"]}', '{"KAT": {"a": 1}}', '{"KAT": true}', '{"QQQ": "x"}', '{"KAT/KAT/KAT/KAT/KAT/KAT/KAT/KAT/KAT/KAT/KAT/KAT/KAT/KAT/KAT/KAT/KAT": "x"}',
                   '{"KAT": "a\\u0000b"}', '{"KAT": "x"}', '{"KAT": "x", "KAT": "x"}', '{"KAT": "x", "KA-T": "y"}', '{"KAT": "x", "KA-T": "x"}',
                   '{"KAT": "' + "x" * 2048 + '"}', '{"KAT": "' + "é" * 1024 + '"}', '{"KAT": "' + "é" * 1023 + '"}', '{"TKOG": "\\ud800"}', "﻿{}",
                   '{"KAT": "café 日本"}', '{"A": "\\ud83d\\ude00"}'):
        add(f"preview whole-dictionary source {source[:40]!r}", [("read", "Custom A", {}), ("change", "Custom A", request(source), False)])
        add(f"preview saved-edits source {source[:40]!r}", [("read", "Custom A", {"mode": "edits"}), ("change", "Custom A", request(source, "edits"), False)])
    add("preview edits: the same outline twice, different edits", [("read", "Custom A", {"mode": "edits"}),
        ("change", "Custom A", request('{"KAT": "x", "KA-T": null}', "edits"), False)])
    add("preview edits: the same outline twice, same edit", [("read", "Custom A", {"mode": "edits"}),
        ("change", "Custom A", request('{"KAT": null, "KA-T": null}', "edits"), False)])
    add("preview edits: a reset and an edit of different outlines", [("read", "Custom A", {"mode": "edits"}),
        ("change", "Custom A", request('{"KAT": null, "TKOG": "dog"}', "edits"), False)])

    save = lambda src, mode="all", **extra: [("read", "Custom A", {"mode": mode}), ("change", "Custom A", request(src, mode, **extra), False),
                                              ("change", "Custom A", request(src, mode, token={"$last": "token"}, **extra), True)]
    for mode in ("all", "edits"):
        src = sources[mode]
        add(f"save an added entry, {mode}", save(edit(src, add={"STKPW": "added"}), mode))
        add(f"save a changed entry, {mode}", save(edit(src, change={"A*PB": "different"} if mode == "all" else {"A": "x"}), mode))
        add(f"save two additions and a removal, {mode}", save(edit(src, add={"STKPW": "added", "TKPWAOEU": "two"}, drop=[sorted(json.loads(src))[0]]), mode))
        add(f"save with no changes, {mode}", save(src, mode))
        add(f"save everything removed, {mode}", save("{}", mode))
    add("save without previewing", [("read", "Custom A", {}), ("change", "Custom A", request(edit(sources["all"], add={"STKPW": "added"}), token="x"), True)])
    add("save with no token", [("read", "Custom A", {}), ("change", "Custom A", request(edit(sources["all"], add={"STKPW": "added"})), True)])
    add("save with a token from another preview", [("read", "Custom A", {}),
        ("change", "Custom A", request(edit(sources["all"], add={"STKPW": "added"})), False),
        ("change", "Custom A", request(edit(sources["all"], add={"TKPWAOEU": "other"}), token={"$last": "token"}), True)])
    changed = edit(sources["all"], add={"STKPW": "added"})
    add("save when the store is read-only", save(changed), statuses=[STATUS, STATUS, STATUS, STATUS, STATUS, {**STATUS, "read_only": True}])
    add("save when the store is not ready", save(changed), statuses=[STATUS, STATUS, STATUS, STATUS, STATUS, {**STATUS, "ready": False}])
    add("save when the status is missing fields", save(changed), statuses=[STATUS, STATUS, STATUS, STATUS, STATUS, {}])
    add("a save is refused partway", save(edit(sources["all"], add={"STKPW": "added", "TKPWAOEU": "two", "TKPWAOEUZ": "three"})),
        overrides={"native_entry commit": ["OK", "ERR Rejected"]})
    add("a removal is refused", save(edit(sources["all"], drop=[sorted(json.loads(sources["all"]))[0]])),
        overrides={"native_remove_entry": ["ERR no such entry"]})
    add("a reset is refused", save(edit(sources["edits"], drop=[sorted(json.loads(sources["edits"]))[0]]), "edits"),
        overrides={"native_reset_entry": ["ERR no such entry"]})
    add("abort is refused after a failed save", save(edit(sources["all"], add={"STKPW": "added"})),
        overrides={"native_entry commit": ["ERR Rejected"], "native_entry abort": ["ERR no"]})
    add("a timeout in a save", save(edit(sources["all"], add={"STKPW": "added"})), overrides={"native_entry commit": ["ERR Native store busy"] * 2})
    # saved edits: resets
    snap_edits = cust_b_edits
    add("save resets on a snapshot dictionary needs firmware support", [("read", "Custom B", {"mode": "edits"}),
        ("change", "Custom B", request("{}", "edits"), False), ("change", "Custom B", request("{}", "edits", token={"$last": "token"}), True)],
        overrides={"native_install update snapshot-reset-capabilities": ["OK RESET0"]})
    add("save resets on a snapshot dictionary", [("read", "Custom B", {"mode": "edits"}),
        ("change", "Custom B", request("{}", "edits"), False), ("change", "Custom B", request("{}", "edits", token={"$last": "token"}), True)])
    add("save resets on a snapshot dictionary with an error reply", [("read", "Custom B", {"mode": "edits"}),
        ("change", "Custom B", request("{}", "edits"), False), ("change", "Custom B", request("{}", "edits", token={"$last": "token"}), True)],
        overrides={"native_install update snapshot-reset-capabilities": ["ERR busy"]})
    add("save resets on a named dictionary needs no capability", [("read", "Custom A", {"mode": "edits"}),
        ("change", "Custom A", request("{}", "edits"), False), ("change", "Custom A", request("{}", "edits", token={"$last": "token"}), True)],
        overrides={"native_install update snapshot-reset-capabilities": ["OK RESET0"]})
    # factory dictionary edits
    add("factory: preview an edit", [("read", base_name, {"mode": "all"}), ("change", base_name, request(edit(base_all, add={"STKPW": "added"}), "all"), False)], factory=factory)
    add("factory: save an edit", [("read", base_name, {"mode": "all"}), ("change", base_name, request(edit(base_all, change={"KAT": "feline"}), "all"), False),
        ("change", base_name, request(edit(base_all, change={"KAT": "feline"}), "all", token={"$last": "token"}), True)], factory=factory)
    add("factory: reset an edit", [("read", base_name, {"mode": "edits"}), ("change", base_name, request("{}", "edits"), False),
        ("change", base_name, request("{}", "edits", token={"$last": "token"}), True)], factory=factory)
    add("factory: save edits", [("read", base_name, {"mode": "edits"}), ("change", base_name, request(edit(base_edits, add={"KAT": "feline", "TKOG": None}), "edits"), False),
        ("change", base_name, request(edit(base_edits, add={"KAT": "feline", "TKOG": None}), "edits", token={"$last": "token"}), True)], factory=factory)
    # ---- gaps found by mutation testing ----
    # the store becomes read-only / not ready only after the preview
    for label, change in (("read-only", {"read_only": True}), ("not ready", {"ready": False}), ("ready and read-only", {"ready": False, "read_only": True}),
                          ("ready is 0", {"ready": 0}), ("read_only is 1", {"read_only": 1}), ("read_only is null", {"read_only": None})):
        src = sources["all"]
        add(f"save, then the store is {label}", [("read", "Custom A", {}), ("change", "Custom A", request(edit(src, add={"STKPW": "added"})), False),
            ("setstatus", [{**STATUS, **change}]), ("change", "Custom A", request(edit(src, add={"STKPW": "added"}), token={"$last": "token"}), True)])
    # undoing saved edits on a snapshot dictionary
    for kind in ("snapshot", "named", "personal", "base"):
        for reply in ("OK RESET1", "OK RESET0", "ERR no", ""):
            add(f"reset edits on a {kind} dictionary, capability {reply!r}", [("read", "Custom A", {"mode": "edits"}, {"kind": kind}),
                ("change", "Custom A", request("{}", "edits"), False, {"kind": kind}),
                ("change", "Custom A", request("{}", "edits", token={"$last": "token"}), True, {"kind": kind})],
                overrides={"native_install update snapshot-reset-capabilities": [reply]})
    add("reset edits on a snapshot dictionary: only additions need no capability", [("read", "Custom A", {"mode": "edits"}, {"kind": "snapshot"}),
        ("change", "Custom A", request(edit(sources["edits"], add={"STKPW": "x"}), "edits"), False, {"kind": "snapshot"}),
        ("change", "Custom A", request(edit(sources["edits"], add={"STKPW": "x"}), "edits", token={"$last": "token"}), True, {"kind": "snapshot"})],
        overrides={"native_install update snapshot-reset-capabilities": ["OK RESET0"]})
    # cache eviction (a dictionary read in both modes keeps two documents)
    order = ["Custom A", "Custom B", "Lapwing Numbers", "Lapwing Commands", "Plover Base", "Lapwing Base"]
    for count in (2, 3, 4):
        reads = [("read", name, {}) for name in order[:count]]
        for probe in order[:count]:
            add(f"after {count} dictionaries, {probe} is read again", reads + [("read", probe, {})])
            add(f"after {count} dictionaries, {probe} is read again in the other mode", reads + [("read", probe, {"mode": "edits"})])
    add("a forced read replaces only its own documents", [("read", "Custom A", {}), ("read", "Custom B", {}), ("read", "Custom A", {"force": True}),
        ("read", "Custom B", {}), ("read", "Custom A", {"mode": "edits"})])
    add("a forced read of one mode drops the other mode too", [("read", "Custom A", {}), ("read", "Custom A", {"mode": "edits"}),
        ("read", "Custom A", {"mode": "all", "force": True}), ("read", "Custom A", {"mode": "edits"})])
    # catalog rows the factory fixture does not produce
    base_row = 6
    for title, field, value in (("marker 1", 11, 1), ("marker 2", 11, 2), ("journal dictionary", 8, 255), ("journal dictionary, high byte", 9, 255),
                                ("hash byte 0 changed", 64, 1), ("hash byte 31 changed", 95, 1), ("hash byte 32 is not part of the row", 96, 1),
                                ("name changed", 16, 90), ("identity changed", 0, 77)):
        patch_bytes = {row_offset(base_row, field): value}
        for mode in ("all", "edits"):
            add(f"factory row patched: {title}, {mode}", [("read", "Lapwing Base", {"mode": mode})], factory=factory, patch=patch_bytes)
    for title, field, value in (("marker 1", 11, 1), ("marker 2", 11, 2), ("marker 3", 11, 3), ("name changed", 16, 90), ("identity changed", 0, 77)):
        patch_bytes = {row_offset(8, field): value}
        for mode in ("all", "edits"):
            add(f"named row patched: {title}, {mode}", [("read", "Custom A", {"mode": mode})], patch=patch_bytes)
    add("a dictionary with a journal base needs no factory file", [("read", "Custom A", {"mode": "all"}, {"kind": "base"})], factory={"Custom A": BASELINE},
        patch={row_offset(8, 8): 255, row_offset(8, 9): 255})
    add("an empty journal dictionary in edits mode", [("read", "Custom A", {"mode": "edits"}, {"kind": "base"})], factory={"Custom A": BASELINE},
        patch={row_offset(8, 11): 0, row_offset(8, 8): 255, row_offset(8, 9): 255})
    # unusual serials
    add("a console without a serial", [("read", "Custom A", {})], serial=None)
    add("a non-ASCII serial", [("read", "Custom A", {})], serial="é日")
    # non-ASCII text in documents
    kib = [0, 1, 63, 64, 127, 128, 192, 255, 256, 257, 320, 384, 511, 512, 513, 640, 767, 768, 769, 896, 1023, 1024, 1025, 1280, 1536, 2048,
           4095, 4096, 8192, 8704, 10240, 10752, 65536, 100000, 1048576, 1048576 + 512, 1024 * 1000 - 1, 1024 * 1000, 1024 * 1000 + 256, 1024 * 12345,
           1024 * 123456 + 768, 1024 * 1234567 + 256, 123456789]
    dump = [{}, {"a": "b"}, {"a": None}, {"é": "日本", "x": "\ud83d\ude00".encode("utf-16", "surrogatepass").decode("utf-16", "surrogatepass")},
            {"tab\there": "line\nbreak", "cr\r": "q\"uote", "back\\slash": "bell\x07", "bs\b": "ff\f", "nul\x00": "us\x1f", "del\x7f": "sp ace"},
            {"k": "x" * 300}, {"1": "one", "2": None}]
    return {"schema": 1, "selections": selections(), "cases": cases,
            "kib": [[n, f"{n / 1024:,.1f}"] for n in kib],
            "dump": [[list(item.items()), json.dumps(item, ensure_ascii=False, indent=2)] for item in dump]}


def main():
    text = json.dumps(generate(), indent=0) + chr(10)
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8").replace(chr(13) + chr(10), chr(10)) != text:
            sys.exit("golden/editor_vectors.json is stale; rerun golden_editor.py")
        print("editor vectors are current")
        return
    OUT.write_text(text, encoding="utf-8", newline=chr(10))
    print(f"wrote {OUT} ({len(text) // 1024} KiB, {len(json.loads(text)['cases'])} scenarios)")


if __name__ == "__main__":
    main()
