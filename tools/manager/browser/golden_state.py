#!/usr/bin/env python3
"""Generate (or check) golden vectors for the JavaScript state reader (state.js, Manager milestone M3).

Oracle: manager.read_state / manager.load_view / manager.installer_view and native_update.overlay_usage. Each case runs the
Python function against a scripted keyboard and records the whole exchange: every console command (with its timeout) and the
reply served, the result or error, the progress messages, the intermediate views shown, and the sleeps. The JavaScript replays
the same transcript and must ask the same questions in the same order and reach the same result.

    python3 tools/manager/browser/golden_state.py            # rewrite golden/state_vectors.json
    python3 tools/manager/browser/golden_state.py --check    # fail if the committed file is stale
"""
import base64
import json
from pathlib import Path
import struct
import sys
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import golden_snapshot as base_vectors  # noqa: F401  (puts the tools/ directories on sys.path)
sys.path.insert(0, str(HERE.parent))
import manager
from build_native_image import SEGMENT, SMALL_POOL_END
from golden_layout import layer
from golden_snapshot import catalog_of, fixture, ownership
from native_install import UNPROVISIONED
from native_update import Snapshot

import random

OUT = HERE / "golden" / "state_vectors.json"
NL = chr(10)
FACTORY = manager.FACTORY_CAPABILITIES
UPDATE = "native_install update "

CATALOG = [{"id": 1, "name": "user_dictionary", "kind": "personal", "enabled": True},
           {"id": 2, "name": "Lapwing Base", "kind": "base", "enabled": True},
           {"id": 9, "name": "Custom A", "kind": "named", "enabled": False}]
STATUS = {"format": "SQBCAT02", "ready": True, "read_only": False, "tail": 5, "generation": 12, "auto_checkpoints": 2,
          "auto_reclaimed": 3, "maintenance": 0}


def base_replies(**overrides):
    replies = {
        "native_install status": "NATIVE2 READY 12",
        "user_dictionary_status": json.dumps(STATUS),
        "get_parameter available_host_layouts": json.dumps(["qwerty", "dvorak"]),
        UPDATE + "snapshot-capabilities": "OK SNAPSHOT2",
        "native_catalog": json.dumps(CATALOG),
        FACTORY: "OK FACTORY2",
        "stroke_hints": "auto",
        "get_parameter host_layout": "qwerty",
        UPDATE + "personal-snapshot-capabilities": "OK PERSONAL1",
        UPDATE + "journal-snapshot-capabilities": "OK JOURNAL1",
        UPDATE + "snapshot-clear-capabilities": "OK CLEAR1",
        UPDATE + "snapshot-merge-capabilities": "OK MERGE1 16",
        UPDATE + "repack-capabilities": "OK REPACK1",
        UPDATE + "layout-capabilities": "OK MAP1",
        UPDATE + "recovery-capabilities": "OK RECOVERY1",
    }
    replies.update(overrides)
    return replies


class Faulty(Exception):
    pass


class Transcript:
    """A scripted keyboard. Replies come from a dict (a list is consumed in order) or a handler; every exchange is recorded."""

    def __init__(self, replies, *, serial="AB12", transport="usb", handler=None):
        self.replies = {key: list(value) if isinstance(value, list) else value for key, value in replies.items()}
        self.serial, self.transport, self.handler, self.log = serial, transport, handler, []

    def command(self, text, timeout=30):
        entry = {"command": text, "timeout": timeout}
        self.log.append(entry)
        try:
            reply = self.answer(text)
        except Exception as error:
            entry["raise"] = {"type": type(error).__name__, "message": str(error)}
            raise
        entry["reply"] = reply
        return reply

    def answer(self, text):
        if self.handler is not None:
            served = self.handler(text)
            if served is not None:
                return served
        if text not in self.replies:
            raise AssertionError(f"unexpected command {text}")
        value = self.replies[text]
        if isinstance(value, list):
            if not value:
                raise AssertionError(f"no more replies for {text}")
            value = value.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class FakeTime:
    def __init__(self):
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)


def outcome(function):
    try:
        return {"value": json.loads(json.dumps(function()))}
    except Exception as error:
        message = "" if isinstance(error, json.JSONDecodeError) else str(error)
        return {"error": {"type": type(error).__name__, "message": message}}


def pack_transcript(console):
    out = []
    for entry in console.log:
        entry = dict(entry)
        reply = entry.get("reply")
        if isinstance(reply, str) and entry["command"].startswith(UPDATE + "read "):
            _, _, _, start, count = entry["command"].split()
            data = base64.b64decode(reply[3:]) if reply.startswith("OK ") else None
            if data is not None:
                entry["reply"] = {"read": data.rstrip(b"\xff").hex(), "count": len(data)}
        out.append(entry)
    return out


def read_state_case(title, replies, *, transport="usb", backup=None, serial="AB12", handler=None):
    console = Transcript(base_replies(**replies), transport=transport, serial=serial, handler=handler)
    original = manager.last_repack_backup
    manager.last_repack_backup = lambda value: SimpleNamespace(name=backup) if backup else None
    try:
        result = outcome(lambda: manager.read_state(console))
    finally:
        manager.last_repack_backup = original
    return {"fn": "readState", "title": title, "transport": transport, "serial": serial, "backup": backup, "expect": result,
            "transcript": pack_transcript(console)}


def installer_case(title, status, replies=None, reprovision=False, transport="usb"):
    console = Transcript({FACTORY: "OK FACTORY2", **(replies or {})}, transport=transport)
    result = outcome(lambda: manager.installer_view(console, status, reprovision=reprovision, transport=transport))
    return {"fn": "installerView", "title": title, "status": status, "reprovision": reprovision, "transport": transport,
            "serial": "AB12", "expect": result, "transcript": pack_transcript(console)}


# ---- a device-shaped keyboard for load_view and overlay_usage ----

class Keyboard:
    """Serves layout maps, image metadata, journal usage and flash reads for the fixture catalog."""

    def __init__(self, image, *, statuses=None, layout=None, usage_caps="OK USAGE2", pages=None, usage_error=None, begin=None):
        self.image = image
        self.address, self.size, self.crc = catalog_of(image)
        probe = Snapshot(lambda a, n: bytes(image[a:a + n]), self.address, self.size, self.crc, metadata_only=True, pool_end=SMALL_POOL_END)
        self.answer = f"OK {self.address} {self.size} {self.crc} {ownership(probe, SMALL_POOL_END)}"
        self.statuses = list(statuses or [])
        self.layout = list(layout or [])
        self.usage_caps, self.pages = usage_caps, pages or {}
        self.begin = list(begin or [])

    def handle(self, text):
        if text == "native_install status":
            return self.statuses.pop(0) if self.statuses else "NATIVE2 READY 12"
        if text == UPDATE + "layout-map":
            if not self.layout:
                raise AssertionError("no more layout replies")
            value = self.layout.pop(0)
            if isinstance(value, BaseException):
                raise value
            return value
        if text == UPDATE + "journal-usage-capabilities":
            return self.usage_caps
        if text in (UPDATE + "begin metadata", UPDATE + "begin backup"):
            if self.begin:
                value = self.begin.pop(0)
                if isinstance(value, BaseException):
                    raise value
                return value
            return self.answer
        if text == UPDATE + "abort":
            return "OK"
        if text.startswith(UPDATE + "read "):
            _, _, _, start, count = text.split()
            return "OK " + base64.b64encode(bytes(self.image[int(start):int(start) + int(count)])).decode()
        if text.startswith(UPDATE + "journal-usage "):
            position = int(text.split()[-1])
            if position not in self.pages:
                raise AssertionError(f"no usage page at {position}")
            value = self.pages[position]
            if isinstance(value, BaseException):
                raise value
            return value
        return None


def fixture_facts(image):
    address, size, crc = catalog_of(image)
    snapshot = Snapshot(lambda a, n: bytes(image[a:a + n]), address, size, crc, metadata_only=True, pool_end=SMALL_POOL_END)
    identities = [struct.unpack_from("<I", row)[0] for row in snapshot.rows]
    journals = sorted(segment for start, span, _, _, kind, _ in snapshot.extents if kind == 2 for segment in range(start, start + span))
    return identities, journals


def usage_pages(identities, journals, *, rows=None, plan=None):
    """USAGE2 pages: a plan is [(position, cursor, limit, [(segment, identity, count, entries, bytes), ...])]."""
    if plan is None:
        first = [(journals[0], identities[0], 3, 2, 100), (journals[0], identities[1], 4, 4, 200), (journals[-1], identities[-1], 5, 0, 300)]
        second = [(journals[-1], identities[0], 8, 1, 400)]
        plan = [(0, 12, 20, first), (12, 20, 20, second)]
    return {position: " ".join(["OK", "USAGE2", str(cursor), str(limit), *[",".join(map(str, row)) if not isinstance(row, str) else row for row in rows_]])
            for position, cursor, limit, rows_ in plan}


def view_case(title, action, keyboard_factory, *, replies=None, transport="usb"):
    image = fixture(SMALL_POOL_END)
    keyboard = keyboard_factory(image)
    console = Transcript(base_replies(**(replies or {})), transport=transport, handler=keyboard.handle)
    original_time, original_backup = manager.time, manager.last_repack_backup
    clock = FakeTime()
    manager.time = clock
    manager.last_repack_backup = lambda value: None
    progress, shown = [], []
    try:
        result = outcome(lambda: manager.load_view(console, action, progress.append, lambda view: shown.append(json.loads(json.dumps(view)))))
    finally:
        manager.time, manager.last_repack_backup = original_time, original_backup
    return {"fn": "loadView", "title": title, "action": action, "transport": transport, "serial": "AB12", "backup": None,
            "expect": result, "progress": progress, "shown": json.loads(json.dumps(shown)), "sleeps": clock.sleeps,
            "transcript": pack_transcript(console)}


def usage_case(title, keyboard_factory, *, metadata_only=False):
    image = fixture(SMALL_POOL_END)
    keyboard = keyboard_factory(image)
    console = Transcript({}, handler=keyboard.handle)
    result = outcome(lambda: manager.overlay_usage(console, metadata_only=metadata_only))
    return {"fn": "overlayUsage", "title": title, "metadataOnly": metadata_only, "serial": "AB12", "transport": "usb", "backup": None,
            "expect": result, "transcript": pack_transcript(console)}


def generate():
    cases = []
    image = fixture(SMALL_POOL_END)
    identities, journals = fixture_facts(image)
    good_pages = usage_pages(identities, journals)

    def keyboard(**options):
        return lambda image_: Keyboard(image_, **options)

    # --- read_state ---
    cases.append(read_state_case("ready, adaptive, writable, USB", {}))
    cases.append(read_state_case("ready over Bluetooth", {}, transport="bluetooth"))
    for key, bad, label in ((UPDATE + "personal-snapshot-capabilities", "OK PERSONAL2", "personal conversion"),
                            (UPDATE + "journal-snapshot-capabilities", "ERR no", "journal conversion"),
                            (UPDATE + "snapshot-clear-capabilities", "OK CLEAR2", "clear"),
                            (UPDATE + "repack-capabilities", "ERR no", "repack"),
                            (UPDATE + "layout-capabilities", "OK MAP2", "layout map"),
                            (FACTORY, "OK FACTORY1", "factory"),
                            (UPDATE + "snapshot-capabilities", "OK SNAPSHOT1", "adaptive")):
        cases.append(read_state_case(f"{label} capability absent", {key: bad}))
    for reply in ("OK MERGE1 7", "OK MERGE1 64", "OK MERGE1 6", "OK MERGE1 65", "OK MERGE1", "OK MERGE2 16", "OK MERGE1 x", "OK MERGE1 16 extra", "ERR no", "  OK  MERGE1  20  "):
        cases.append(read_state_case(f"merge capability {reply!r}", {UPDATE + "snapshot-merge-capabilities": reply}))
    for hints in ("auto", "off", "on", "", "AUTO"):
        cases.append(read_state_case(f"hints {hints!r}", {"stroke_hints": hints}))
    for ro in (True, 1, "yes", 0, None, [], {}, "", False):
        cases.append(read_state_case(f"read_only {ro!r}", {"user_dictionary_status": json.dumps({**STATUS, "read_only": ro})}))
    for maintenance in (1, True, 0, None, "", [], "x"):
        cases.append(read_state_case(f"maintenance {maintenance!r}", {"user_dictionary_status": json.dumps({**STATUS, "maintenance": maintenance})}))
    cases.append(read_state_case("status without optional fields",
                                 {"user_dictionary_status": json.dumps({"format": "SQBCAT02", "ready": True, "read_only": False, "tail": 0})}))
    for drop in ("read_only", "tail"):
        cases.append(read_state_case(f"status missing {drop}", {"user_dictionary_status": json.dumps({k: v for k, v in STATUS.items() if k != drop})}))
    for title, status in (("wrong format", {**STATUS, "format": "SQBCAT01"}), ("not ready", {**STATUS, "ready": False}),
                          ("ready is zero", {**STATUS, "ready": 0}), ("ready is a string", {**STATUS, "ready": "yes"}),
                          ("ready is missing", {k: v for k, v in STATUS.items() if k != "ready"}),
                          ("a list", [1, 2]), ("a string", "x"), ("null", None), ("a number", 7)):
        cases.append(read_state_case(f"user dictionary status: {title}", {"user_dictionary_status": json.dumps(status)}))
    cases.append(read_state_case("user dictionary status is not JSON", {"user_dictionary_status": "garbage"}))
    cases.append(read_state_case("user dictionary status is an error line", {"user_dictionary_status": "ERR busy"}))
    cases.append(read_state_case("layouts are not JSON", {"get_parameter available_host_layouts": "qwerty"}))
    cases.append(read_state_case("layouts are a string", {"get_parameter available_host_layouts": '"qwerty"'}))
    cases.append(read_state_case("catalog is not JSON", {"native_catalog": "nope"}))
    cases.append(read_state_case("catalog is empty", {"native_catalog": "[]"}))
    cases.append(read_state_case("catalog is an object", {"native_catalog": "{}"}))
    cases.append(read_state_case("catalog is a string", {"native_catalog": '"x"'}))
    cases.append(read_state_case("catalog is null", {"native_catalog": "null"}))
    for status in ("ERR Native flash busy", "NATIVE2 INSTALL 40", "NATIVE2 INSTALL", "NATIVE2 BOGUS", "", "NATIVE2 READY", "NATIVE2 READY 12 extra",
                   "native2 ready 12", "NATIVE2 READYX 12"):
        cases.append(read_state_case(f"status {status!r}", {"native_install status": status}))
    cases.append(read_state_case("unprovisioned, factory installer", {"native_install status": UNPROVISIONED}))
    cases.append(read_state_case("unprovisioned over Bluetooth", {"native_install status": UNPROVISIONED}, transport="bluetooth"))
    cases.append(read_state_case("unprovisioned with other generation", {"native_install status": "NATIVE2 UNPROVISIONED 5"}))
    cases.append(read_state_case("unprovisioned with a repack backup", {"native_install status": UNPROVISIONED}, backup="plan1"))
    cases.append(read_state_case("unprovisioned backup without recovery capability",
                                 {"native_install status": UNPROVISIONED, UPDATE + "recovery-capabilities": "OK RECOVERY2"}, backup="plan1"))
    cases.append(read_state_case("unprovisioned backup over Bluetooth", {"native_install status": UNPROVISIONED}, backup="plan1", transport="bluetooth"))
    cases.append(read_state_case("unprovisioned other generation with a backup",
                                 {"native_install status": "NATIVE2 UNPROVISIONED 5"}, backup="plan1"))
    cases.append(read_state_case("unprovisioned backup, no factory capability",
                                 {"native_install status": UNPROVISIONED, FACTORY: "OK FACTORY1"}))
    cases.append(read_state_case("a different serial", {}, serial="ZZ99"))
    cases.append(read_state_case("a console timeout on the catalog", {"native_catalog": TimeoutError("No reply to native_catalog. Check the left half’s connection.")}))
    cases.append(read_state_case("an unexpected command", {"stroke_hints": [AssertionError("boom")]}))

    cases.append(read_state_case("a status that is only the unprovisioned word", {"native_install status": "NATIVE2 UNPROVISIONED"}))
    cases.append(read_state_case("a status that runs on from the unprovisioned word", {"native_install status": "NATIVE2 UNPROVISIONEDX 1"}))
    cases.append(read_state_case("user dictionary status times out", {"user_dictionary_status": TimeoutError("No reply to user_dictionary_status. Check the left half’s connection.")}))
    cases.append(read_state_case("merge capability with the wrong marker", {UPDATE + "snapshot-merge-capabilities": "NO MERGE1 16"}))
    cases.append(read_state_case("merge capability with a non-numeric limit", {UPDATE + "snapshot-merge-capabilities": "OK MERGE1 x"}))
    # --- installer_view ---
    for status in ("NATIVE2 READY 12", UNPROVISIONED, "NATIVE2 UNPROVISIONED 5 extra words", "NATIVE2 UNPROVISIONED 5 ", "NATIVE2 UNPROVISIONED",
                   "NATIVE2 INSTALL 3", "garbage", "", "NATIVE2 READY 12\n", "NATIVE2 UNPROVISIONED 1" + NL + "x"):
        cases.append(installer_case(f"installer for {status!r}", status))
    cases.append(installer_case("installer reprovision over Bluetooth", "NATIVE2 READY 3", reprovision=True, transport="bluetooth"))
    cases.append(installer_case("installer without factory capability", UNPROVISIONED, {FACTORY: "OK FACTORY1"}))

    # --- load_view ---
    cases.append(view_case("refresh: layout map and usage", "refresh", keyboard(layout=[f"OK MAP1 1 w vv {layer(random.Random(3), SMALL_POOL_END, 'v')} {layer(random.Random(4), SMALL_POOL_END, 'v')}"], pages=good_pages)))
    for action in ("enable", "hints", "layout", "preview_compact", "export", "open_editor", "unknown", "setup", "apply_editor", "delete"):
        cases.append(view_case(f"action {action!r}", action, keyboard(layout=[f"OK MAP1 1 w vv {layer(random.Random(3), SMALL_POOL_END, 'v')} {layer(random.Random(4), SMALL_POOL_END, 'v')}"], pages=good_pages)))
    cases.append(view_case("no layout map capability", "refresh", keyboard(pages=good_pages), replies={UPDATE + "layout-capabilities": "OK MAP2"}))
    cases.append(view_case("not adaptive (no layout map, backup usage)", "refresh", keyboard(usage_caps="OK USAGE1"),
                           replies={UPDATE + "snapshot-capabilities": "OK SNAPSHOT1"}))
    cases.append(view_case("layout scan deferred then ready", "refresh", keyboard(
        layout=["ERR Layout scan deferred until the flash and input are idle", f"OK MAP1 1 w vv {layer(random.Random(3), SMALL_POOL_END, 'v')} {layer(random.Random(4), SMALL_POOL_END, 'v')}"], pages=good_pages)))
    cases.append(view_case("layout scan deferred with another reason then ready", "refresh", keyboard(
        layout=["ERR Layout scan deferred by input", f"OK MAP1 1 w vv {layer(random.Random(3), SMALL_POOL_END, 'v')} {layer(random.Random(4), SMALL_POOL_END, 'v')}"], pages=good_pages)))
    cases.append(view_case("layout scan deferred three times", "refresh", keyboard(
        layout=["ERR Layout scan deferred until the flash and input are idle"] * 3, pages=good_pages)))
    cases.append(view_case("layout scan deferred twice then ready", "refresh", keyboard(
        layout=["ERR Layout scan deferred until the flash and input are idle"] * 2 + [f"OK MAP1 1 w vv {layer(random.Random(3), SMALL_POOL_END, 'v')} {layer(random.Random(4), SMALL_POOL_END, 'v')}"], pages=good_pages)))
    cases.append(view_case("layout scan fails otherwise", "refresh", keyboard(layout=["ERR Something else"], pages=good_pages)))
    cases.append(view_case("layout map is invalid", "refresh", keyboard(layout=["OK MAP1 nonsense"], pages=good_pages)))
    cases.append(view_case("layout scan times out", "refresh", keyboard(layout=[TimeoutError("No reply to native_install. Check the left half’s connection.")], pages=good_pages)))
    cases.append(view_case("layout scan is an unexpected failure", "refresh", keyboard(layout=[KeyError("odd")], pages=good_pages)))
    cases.append(view_case("status changes before the usage is attached", "refresh", keyboard(
        statuses=["NATIVE2 READY 12", "NATIVE2 READY 13"], layout=[f"OK MAP1 1 w vv {layer(random.Random(3), SMALL_POOL_END, 'v')} {layer(random.Random(4), SMALL_POOL_END, 'v')}"], pages=good_pages)))
    cases.append(view_case("usage busy", "refresh", keyboard(usage_caps="ERR Native flash busy", layout=[f"OK MAP1 1 w vv {layer(random.Random(3), SMALL_POOL_END, 'v')} {layer(random.Random(4), SMALL_POOL_END, 'v')}"])))
    cases.append(view_case("usage capabilities error", "refresh", keyboard(usage_caps="ERR no")))
    heavy = [(0 if False else position * 128, position * 128 + 128, 33 * 128, [(journals[0], identities[0], 128, 128, 1000)]) for position in range(33)]
    cases.append(view_case("journal budget exceeded", "refresh", keyboard(pages=usage_pages(identities, journals, plan=heavy),
                                                                      layout=[f"OK MAP1 1 w vv {layer(random.Random(3), SMALL_POOL_END, 'v')} {layer(random.Random(4), SMALL_POOL_END, 'v')}"])))

    layout_ok = f"OK MAP1 1 w vv {layer(random.Random(3), SMALL_POOL_END, 'v')} {layer(random.Random(4), SMALL_POOL_END, 'v')}"
    cases.append(view_case("image details refused with an error line", "refresh", keyboard(begin=["ERR no", None], layout=[layout_ok], pages=good_pages)))
    cases.append(view_case("image details fail with an unexpected error", "refresh", keyboard(begin=[KeyError("odd")], layout=[layout_ok], pages=good_pages)))
    cases.append(view_case("image details time out", "refresh", keyboard(begin=[TimeoutError("No reply to native_install. Check the left half’s connection.")], layout=[layout_ok], pages=good_pages)))
    cases.append(view_case("layout scan times out with deferred-looking text", "refresh", keyboard(layout=[TimeoutError("ERR Layout scan deferred by a timeout")], pages=good_pages)))
    cases.append(view_case("usage raises an unexpected error", "refresh", keyboard(pages={0: KeyError("odd")}, layout=[layout_ok])))
    cases.append(view_case("usage raises a timeout", "refresh", keyboard(pages={0: TimeoutError("No reply to native_install. Check the left half’s connection.")}, layout=[layout_ok])))
    no_generation = {"user_dictionary_status": json.dumps({k: v for k, v in STATUS.items() if k != "generation"})}
    cases.append(view_case("no generation, status matches the literal None", "refresh", keyboard(statuses=["NATIVE2 READY 12", "NATIVE2 READY None"], layout=[layout_ok], pages=good_pages), replies=no_generation))
    cases.append(view_case("no generation, status matches a number", "refresh", keyboard(statuses=["NATIVE2 READY 12", "NATIVE2 READY 12"], layout=[layout_ok], pages=good_pages), replies=no_generation))
    for total in (4095, 4096, 4097):
        full, rest = divmod(total, 128)
        plan = [(n * 128, n * 128 + 128, total, [(journals[0], identities[0], 128, 1, 10)]) for n in range(full)]
        if rest:
            plan.append((full * 128, full * 128 + rest, total, [(journals[0], identities[0], rest, 1, 10)]))
        cases.append(view_case(f"journal budget of {total} records", "refresh", keyboard(pages=usage_pages(identities, journals, plan=plan), layout=[layout_ok])))
    cases.append(view_case("Bluetooth refresh", "refresh", keyboard(pages=good_pages, layout=[f"OK MAP1 1 w vv {layer(random.Random(3), SMALL_POOL_END, 'v')} {layer(random.Random(4), SMALL_POOL_END, 'v')}"]), transport="bluetooth"))
    cases.append(view_case("setup view skips the map", "refresh", keyboard(statuses=[UNPROVISIONED])))
    cases.append(view_case("setup view over Bluetooth", "refresh", keyboard(statuses=[UNPROVISIONED]), transport="bluetooth"))
    cases.append(view_case("read-only keyboard", "refresh", keyboard(pages=good_pages, layout=[f"OK MAP1 1 r vv {layer(random.Random(3), SMALL_POOL_END, 'v')} {layer(random.Random(4), SMALL_POOL_END, 'v')}"]),
                           replies={"user_dictionary_status": json.dumps({**STATUS, "read_only": True})}))
    cases.append(view_case("a state failure", "refresh", keyboard(), replies={"native_catalog": "[]"}))

    # --- overlay_usage on its own ---
    cases.append(usage_case("on-device usage, two pages", keyboard(pages=good_pages)))
    cases.append(usage_case("on-device usage, metadata only", keyboard(pages=good_pages), metadata_only=True))
    cases.append(usage_case("backup-style usage", keyboard(usage_caps="OK USAGE1")))
    cases.append(usage_case("backup-style usage, metadata only refused", keyboard(usage_caps="OK USAGE1"), metadata_only=True))
    cases.append(usage_case("capability busy", keyboard(usage_caps="ERR Native flash busy")))
    cases.append(usage_case("capability busy, metadata only", keyboard(usage_caps="ERR Native flash busy"), metadata_only=True))
    ids, js = identities, journals
    rows = lambda *items: items

    def plan_case(title, plan, metadata_only=False):
        cases.append(usage_case(title, keyboard(pages=usage_pages(ids, js, plan=plan)), metadata_only=metadata_only))

    ok = (js[0], ids[0], 3, 2, 100)
    plan_case("one empty page", [(0, 0, 0, [])])
    plan_case("a page with a single row", [(0, 3, 3, [ok])])
    plan_case("two rows same segment, different identity", [(0, 7, 7, [ok, (js[0], ids[1], 4, 4, 50)])])
    plan_case("the same pair on two pages is fine", [(0, 3, 6, [ok]), (3, 6, 6, [ok])])
    plan_case("cursor beyond the limit", [(0, 9, 5, [ok])])
    plan_case("cursor before the position", [(0, 3, 6, [ok]), (3, 2, 6, [])])
    plan_case("cursor stalls short of the limit", [(0, 0, 5, [])])
    plan_case("cursor stalls at the limit", [(0, 0, 0, [])])
    plan_case("a page larger than 128", [(0, 129, 129, [(js[0], ids[0], 129, 1, 10)])])
    plan_case("a page of exactly 128", [(0, 128, 128, [(js[0], ids[0], 128, 1, 10)])])
    plan_case("the limit changes between pages", [(0, 3, 6, [ok]), (3, 6, 7, [ok])])
    plan_case("a segment that is not a journal", [(0, 3, 3, [(0, ids[0], 3, 2, 100)])])
    plan_case("an unknown identity", [(0, 3, 3, [(js[0], 9999, 3, 2, 100)])])
    plan_case("a duplicate pair on one page", [(0, 6, 6, [ok, ok])])
    plan_case("entries above the count", [(0, 3, 3, [(js[0], ids[0], 3, 4, 100)])])
    plan_case("entries negative", [(0, 3, 3, [(js[0], ids[0], 3, -1, 100)])])
    plan_case("entries equal to the count", [(0, 3, 3, [(js[0], ids[0], 3, 3, 100)])])
    plan_case("zero entries", [(0, 3, 3, [(js[0], ids[0], 3, 0, 100)])])
    plan_case("a zero count", [(0, 3, 3, [(js[0], ids[0], 0, 0, 100)])])
    plan_case("a count larger than the page", [(0, 3, 3, [(js[0], ids[0], 4, 2, 100)])])
    plan_case("counts that sum past the page", [(0, 4, 4, [(js[0], ids[0], 3, 2, 100), (js[0], ids[1], 3, 2, 100)])])
    plan_case("counts that sum to the page", [(0, 6, 6, [(js[0], ids[0], 3, 2, 100), (js[0], ids[1], 3, 2, 100)])])
    plan_case("zero bytes", [(0, 3, 3, [(js[0], ids[0], 3, 2, 0)])])
    plan_case("bytes equal to a segment", [(0, 3, 3, [(js[0], ids[0], 3, 2, SEGMENT)])])
    plan_case("bytes over a segment", [(0, 3, 3, [(js[0], ids[0], 3, 2, SEGMENT + 1)])])
    plan_case("a row with too few fields", [(0, 3, 3, [f"{js[0]},{ids[0]},3,2"])])
    plan_case("a row with too many fields", [(0, 3, 3, [f"{js[0]},{ids[0]},3,2,100,7"])])
    plan_case("a row with a bad number", [(0, 3, 3, [f"{js[0]},{ids[0]},x,2,100"])])
    plan_case("a row with a bad number after the fifth", [(0, 3, 3, [f"{js[0]},{ids[0]},3,2,100,y"])])
    plan_case("a row with a bad number past the sixth", [(0, 3, 3, [f"{js[0]},{ids[0]},3,2,100,7,y"])])
    plan_case("a row with an empty field", [(0, 3, 3, [f"{js[0]},{ids[0]},,2,100"])])
    plan_case("a row with a signed number", [(0, 3, 3, [f"+{js[0]},{ids[0]},3,2,100"])])
    plan_case("a row with a negative segment", [(0, 3, 3, [f"-{js[0]},{ids[0]},3,2,100"])])
    plan_case("a bad cursor", [(0, "x", 3, [])])
    plan_case("the same bytes in the same block add up", [(0, 6, 6, [(js[0], ids[0], 3, 2, 100), (js[0], ids[1], 3, 2, 200)])])
    for name, pages in (("a wrong marker", {0: "NO USAGE2 3 3 " + f"{js[0]},{ids[0]},3,2,100"}), ("a wrong version", {0: "OK USAGE3 3 3 " + f"{js[0]},{ids[0]},3,2,100"}),
                        ("too few tokens", {0: "OK USAGE2 3"}), ("an error", {0: "ERR boom"}), ("an empty reply", {0: "OK"})):
        cases.append(usage_case(f"usage page with {name}", keyboard(pages=pages)))
    return {"schema": 1, "cases": cases}


def main():
    text = json.dumps(generate(), indent=0) + chr(10)
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8").replace(chr(13) + chr(10), chr(10)) != text:
            sys.exit("golden/state_vectors.json is stale; rerun golden_state.py")
        print("state vectors are current")
        return
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline=chr(10))
    print(f"wrote {OUT} ({len(text) // 1024} KiB, {len(json.loads(text)['cases'])} cases)")


if __name__ == "__main__":
    main()
