#!/usr/bin/env python3
"""Generate (or check) golden vectors for the JavaScript job orchestration (jobs.js + manage.js, Manager milestone M3).

Oracle: manager.run_job (with setup_keyboard, read_import, safe_name), native_install.wait_ready and native_manage.checked /
commit_with_progress / import_entries, run against a scripted world: console replies, session openers, a deterministic clock.
The heavy operations that have their own ports (factory_package, change_snapshot, merge_snapshots, named_import, transfer,
the editor, repack and recovery, export, backup, provisioning) are replaced by recording fakes. Each case records ONE ordered
event stream: every session opened and closed, console command (with timeout and reply), fake call (with its arguments and
what it returned), progress message, view shown and sleep, so the JavaScript must do the same things in the same order.

    python3 tools/manager/browser/golden_jobs.py            # rewrite golden/jobs_vectors.json
    python3 tools/manager/browser/golden_jobs.py --check    # fail if the committed file is stale
"""
from contextlib import contextmanager
import json
from pathlib import Path
import random
import re
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import golden_snapshot as base_vectors  # noqa: F401  (puts the tools/ directories on sys.path)
sys.path.insert(0, str(HERE.parent))
import manager
import native_install
import native_manage
from build_native_image import SMALL_POOL_END
from golden_layout import layer
from golden_snapshot import fixture
from golden_state import CATALOG, FACTORY, STATUS, UPDATE, Keyboard, base_replies, fixture_facts, usage_pages
from native_install import UNPROVISIONED

OUT = HERE / "golden" / "jobs_vectors.json"
NL = chr(10)
ROWS = CATALOG + [{"id": 10, "name": "Snap One", "kind": "snapshot", "enabled": True},
                  {"id": 11, "name": "Plover Base", "kind": "uninstalled", "enabled": False}]
LAYOUT_OK = f"OK MAP1 1 w vv {layer(random.Random(3), SMALL_POOL_END, 'v')} {layer(random.Random(4), SMALL_POOL_END, 'v')}"
IMAGE = fixture(SMALL_POOL_END)
IDENTITIES, JOURNALS = fixture_facts(IMAGE)
GOOD_PAGES = usage_pages(IDENTITIES, JOURNALS)
TOKEN = "a" * 64


def err(error):
    message = "" if isinstance(error, json.JSONDecodeError) else str(error)
    return {"type": type(error).__name__, "message": message}


class World:
    """One scripted keyboard shared by every session, with a deterministic clock and one ordered event log."""

    def __init__(self, replies=None, *, serial="AB12", transport="usb", keyboard=None, returns=None, opens=None, costs=None):
        self.serial, self.transport = serial, transport
        self.events, self.now = [], 1000.0
        self.replies = {key: list(value) if isinstance(value, list) else value
                        for key, value in base_replies(**{"native_catalog": json.dumps(ROWS), UPDATE + "manage-capabilities": "OK MANAGE1",
                                                           "native_entry abort": "OK",
                                                           "native_entry commit": "OK", "enable_dictionary *": "OK", "disable_dictionary *": "OK",
                                                           "stroke_hints *": "OK", "set_host_layout *": "OK", "native_remove_entry *": "OK",
                                                           "native_reset_entry *": "OK", **(replies or {})}).items()}
        self.keyboard = keyboard
        self.returns = {key: list(value) if isinstance(value, list) else value for key, value in (returns or {}).items()}
        self.opens = list(opens or [])
        self.costs = costs or {}

    # -- clock --
    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.events.append({"e": "sleep", "s": seconds})
        self.now += seconds

    # -- sessions --
    def opener(self, via):
        world = self

        @contextmanager
        def open_session(serial=None):
            event = {"e": "open", "via": via, "serial": serial}
            world.events.append(event)
            script = world.opens.pop(0) if world.opens else None
            if isinstance(script, BaseException):
                event["raise"] = err(script)
                raise script
            if serial and serial != world.serial:
                error = RuntimeError("The selected keyboard identity differs. Connect the intended Steno Squid left half.")
                event["raise"] = err(error)
                raise error
            console = WorldConsole(world)
            try:
                yield console
            finally:
                world.events.append({"e": "close"})
        return open_session

    # -- fakes --
    def fake(self, name, describe, default=None):
        def call(*args, **kwargs):
            event = {"e": "call", "n": name, "a": describe(*args, **kwargs)}
            self.events.append(event)
            value = self.returns.get(name, default)
            if isinstance(value, list):
                value = value.pop(0)
            if callable(value):
                value = value(*args, **kwargs)
            if isinstance(value, BaseException):
                event["raise"] = err(value)
                raise value
            event["ret"] = plain(value)
            return value
        return call


class WorldConsole:
    def __init__(self, world):
        self.world, self.serial, self.transport = world, world.serial, world.transport

    def command(self, text, timeout=30):
        world = self.world
        event = {"e": "cmd", "c": text, "t": timeout}
        world.events.append(event)
        try:
            reply = self.answer(text)
        except Exception as error:
            event["r"] = {"raise": err(error)}
            event["dt"] = world.costs.get("*", 0.001)
            world.now += event["dt"]
            raise
        event["r"] = reply
        event["dt"] = next((cost(text, reply) if callable(cost) else cost for prefix, cost in world.costs.items()
                            if prefix != "*" and text.startswith(prefix)), world.costs.get("*", 0.001))
        world.now += event["dt"]
        return reply

    def answer(self, text):
        world = self.world
        if world.keyboard is not None:
            served = world.keyboard.handle(text)
            if served is not None:
                return served
        for prefix in ("native_entry begin ", "native_entry data "):
            if text.startswith(prefix) and prefix not in world.replies:
                return "OK"
        key = text if text in world.replies else next((prefix for prefix in world.replies if prefix.endswith("*") and text.startswith(prefix[:-1])), None)
        if key is None:
            raise AssertionError(f"unexpected command {text}")
        value = world.replies[key]
        if isinstance(value, list):
            if not value:
                raise AssertionError(f"no more replies for {text}")
            value = value.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def entries_of(entries):
    return None if entries is None else [[list(strokes), text.hex()] for strokes, text in entries.items()]


def install(world):
    """Patch manager.py and its helpers to the scripted world; returns an undo function."""
    patches = {
        (manager, "open_console"): world.opener("usb"), (manager, "open_bluetooth_console"): world.opener("bluetooth"),
        (native_install, "open_console"): world.opener("usb"),
        (manager, "time"): world, (native_install, "time"): world, (native_manage, "time"): world,
        (manager, "last_repack_backup"): lambda serial: None,
        (manager, "clear_documents"): world.fake("clear_documents", lambda serial: {"serial": serial}),
        (manager, "read_document"): world.fake("read_document", lambda console, selected, progress, force=False, mode="all": {"selected": selected, "force": force, "mode": mode}, {"doc": 1}),
        (manager, "change_document"): world.fake("change_document", lambda console, selected, request, progress, apply=False: {"selected": selected, "request": request, "apply": apply}, {"plan": 1}),
        (manager, "preview_repack"): world.fake("preview_repack", lambda console, progress: {}, {"repack": 1}),
        (manager, "capture_approved_repack"): world.fake("capture_approved_repack", lambda console, request, progress: {"request": request}, ("src", "plan")),
        (manager, "install_repack"): world.fake("install_repack", lambda source, plan, progress: {"source": source, "plan": plan}),
        (manager, "restore_original"): world.fake("restore_original", lambda serial, request, progress: {"serial": serial, "request": request}),
        (manager, "capture_approved_merge"): world.fake("capture_approved_merge", lambda console, request, progress: {"request": request}, ("msrc", "morig")),
        (manager, "install_offline_merge"): world.fake("install_offline_merge", lambda source, original, request, progress: {"source": source, "original": original, "request": request}, "Merged Name"),
        (manager, "factory_package"): world.fake("factory_package", lambda console, package, restore=None, confirmation=None, progress=None: {"package": package, "restore": restore, "confirmation": confirmation}, {"pkg": 1}),
        (manager, "change_snapshot"): world.fake("change_snapshot", lambda console, operation, name, stage=False, preview=False, expected=None, target_id=None, progress=None: {"operation": operation, "name": name, "stage": stage, "preview": preview, "expected": expected, "target_id": target_id}, {"snap": 1}),
        (manager, "clear_personal"): world.fake("clear_personal", lambda console, identity, progress: {"id": identity}, True),
        (manager, "merge_snapshots"): world.fake("merge_snapshots", lambda console, upper, lower, expected=None, progress=None: {"upper": upper, "lower": lower, "expected": expected}, {"merge": 1}),
        (manager, "named_import"): world.fake("named_import", lambda console, name, entries, stage=False, expected=None, progress=None: {"name": name, "entries": entries_of(entries), "stage": stage, "expected": expected}, {"import": 1}),
        (manager, "transfer"): world.fake("transfer", lambda console, entries=None, progress=None, **options: {"entries": entries_of(entries), "options": options}, True),
        (manager, "export_dictionary"): world.fake("export_dictionary", lambda console, name: {"name": name}, {"KAT": "cat"}),
        (manager, "backup_all"): world.fake("backup_all", lambda console: {}, {"backup": 1}),
        (manager, "prepare_install_image"): world.fake("prepare_install_image", lambda packages=None: {"packages": packages}, (b"IMG", {"m": 1})),
        (manager, "provision"): world.fake("provision", lambda console, image, manifest, progress, expected_status=None: {"image": image.hex(), "manifest": manifest, "expected_status": expected_status}),
    }
    originals = {key: getattr(key[0], key[1]) for key in patches}
    for (module, name), value in patches.items():
        setattr(module, name, value)

    def undo():
        for (module, name), value in originals.items():
            setattr(module, name, value)
    return undo


def plain(value):
    """A call's return value as JSON: tuples become lists and bytes become {"bytes": hex}."""
    if isinstance(value, (bytes, bytearray)):
        return {"bytes": bytes(value).hex()}
    if isinstance(value, (tuple, list)):
        return [plain(item) for item in value]
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    return value


def jsonable(value):
    return json.loads(json.dumps(value))


def job_case(title, request, *, replies=None, transport="usb", serial="AB12", keyboard=True, **options):
    kb = Keyboard(IMAGE, layout=[LAYOUT_OK] * 6, pages=GOOD_PAGES) if keyboard is True else keyboard
    world = World(replies, serial=options.pop("world_serial", "AB12"), transport=transport, keyboard=kb, **options)
    undo = install(world)
    try:
        def progress(message):
            world.events.append({"e": "progress", "m": message})

        def show(view):
            world.events.append({"e": "view", "v": jsonable(view)})
        try:
            result = {"value": jsonable(manager.run_job(serial, request, progress, show, transport=transport))}
        except Exception as error:
            result = {"error": err(error)}
            tail = re.search(r"(saved entries: ).*line [0-9]+ column [0-9]+ [(]char [0-9]+[)]$", result["error"]["message"])
            if tail:
                result["error"]["message"] = result["error"]["message"][:tail.end(1)] + "<json error>"
    finally:
        undo()
    return {"title": title, "serial": serial, "transport": transport, "request": request, "expect": result, "events": world.events}


def generate():
    cases = []

    def add(title, request, **options):
        cases.append(job_case(title, request, **options))

    # ---- transport restrictions ----
    for action in ("preview_repack", "apply_repack", "restore_repack_backup", "preview_merge", "apply_merge", "apply_offline_merge",
                   "preview_import", "apply_import", "import"):
        add(f"{action} over Bluetooth", {"action": action}, transport="bluetooth")
    add("enable over Bluetooth", {"action": "enable", "id": 1, "enabled": True}, transport="bluetooth")
    add("refresh over Bluetooth", {"action": "refresh"}, transport="bluetooth")

    # ---- the simple reads ----
    add("refresh", {"action": "refresh"})
    add("layout_map", {"action": "layout_map"})
    add("open_installer", {"action": "open_installer"}, replies={"native_install status": "NATIVE2 READY 12"})
    add("open_installer, unprovisioned", {"action": "open_installer"}, keyboard=Keyboard(IMAGE, statuses=[UNPROVISIONED]))
    add("open_installer over Bluetooth", {"action": "open_installer"}, transport="bluetooth")
    add("preview_repack", {"action": "preview_repack"})
    add("a request with no action", {})
    add("an unknown action", {"action": "teleport"})
    add("an unknown action with a selection", {"action": "teleport", "id": 1})
    add("a different serial", {"action": "refresh"}, serial="ZZ99")

    # ---- preview_compact ----
    add("preview_compact with a flash map", {"action": "preview_compact"}, replies={"user_dictionary_status": json.dumps(STATUS)})
    add("preview_compact without the map capability", {"action": "preview_compact"}, replies={UPDATE + "layout-capabilities": "OK MAP2"})
    add("preview_compact: layout scan refused", {"action": "preview_compact"}, keyboard=Keyboard(IMAGE, layout=["ERR no"], pages=GOOD_PAGES))
    add("preview_compact: layout map invalid", {"action": "preview_compact"}, keyboard=Keyboard(IMAGE, layout=["OK MAP1 nonsense"], pages=GOOD_PAGES))
    add("preview_compact: layout scan times out", {"action": "preview_compact"}, keyboard=Keyboard(IMAGE, layout=[TimeoutError("No reply to native_install. Check the left half’s connection.")], pages=GOOD_PAGES))
    add("preview_compact: generation changes", {"action": "preview_compact"},
        replies={"user_dictionary_status": [json.dumps(STATUS), json.dumps({**STATUS, "generation": 13})]})
    add("preview_compact: status without a generation", {"action": "preview_compact"},
        replies={"user_dictionary_status": json.dumps({k: v for k, v in STATUS.items() if k != "generation"})})
    add("preview_compact: status is not JSON", {"action": "preview_compact"}, replies={"user_dictionary_status": "garbage"})
    add("preview_compact: cleanup refused", {"action": "preview_compact"}, returns={"transfer": ValueError("Nothing to clean")})
    add("preview_compact: image details refused", {"action": "preview_compact"},
        keyboard=Keyboard(IMAGE, layout=[LAYOUT_OK], pages=GOOD_PAGES, begin=["ERR no"]))

    # ---- selection and install checks ----
    for action, extra in (("enable", {"enabled": True}), ("export", {}), ("set", {"outline": "KAT", "translation": "cat"}), ("remove", {"outline": "KAT"}),
                          ("reset", {"outline": "KAT"}), ("open_editor", {}), ("preview_editor", {}), ("apply_editor", {}),
                          ("move", {"position": 1}), ("rename", {"name": "X"}), ("delete", {"confirm": "x"}),
                          ("preview_optimize", {}), ("apply_optimize", {"token": TOKEN})):
        add(f"{action} with no selection", {"action": action, **extra})
        add(f"{action} with an unknown id", {"action": action, "id": 99, **extra})
    for action, extra in (("enable", {"enabled": True}), ("export", {}), ("set", {"outline": "KAT", "translation": "cat"}), ("remove", {"outline": "KAT"}),
                          ("reset", {"outline": "KAT"}), ("open_editor", {}), ("preview_editor", {}), ("apply_editor", {})):
        add(f"{action} on an uninstalled dictionary", {"action": action, "id": 11, **extra})
    add("a catalog row without a name", {"action": "enable", "id": 5, "enabled": True},
        replies={"native_catalog": json.dumps([{"id": 5, "kind": "named"}])})
    add("a catalog row without an id", {"action": "enable", "id": 5, "enabled": True},
        replies={"native_catalog": json.dumps([{"name": "x", "kind": "named"}])})
    add("a catalog row without a kind", {"action": "enable", "id": 5, "enabled": True},
        replies={"native_catalog": json.dumps([{"id": 5, "name": "x"}])})
    for name in ("", "has\nnewline", "has\rreturn", "has\0nul", 7, None):
        add(f"an unsafe dictionary name {name!r}", {"action": "enable", "id": 5, "enabled": True},
            replies={"native_catalog": json.dumps([{"id": 5, "name": name, "kind": "named"}])})
    add("a duplicate id keeps the last row", {"action": "enable", "id": 1, "enabled": True},
        replies={"native_catalog": json.dumps([{"id": 1, "name": "first", "kind": "named"}, {"id": 1, "name": "second", "kind": "named"}])})
    add("an empty catalog", {"action": "enable", "id": 1, "enabled": True}, replies={"native_catalog": "[]"})

    # ---- enable / hints / layout ----
    add("enable", {"action": "enable", "id": 9, "enabled": True})
    add("disable", {"action": "enable", "id": 9, "enabled": False})
    for value in ("yes", 1, 0, None, []):
        add(f"enable with {value!r}", {"action": "enable", "id": 9, "enabled": value})
    add("enable without the flag", {"action": "enable", "id": 9})
    add("enable refused by the keyboard", {"action": "enable", "id": 9, "enabled": True}, replies={"enable_dictionary Custom A": "ERR No such dictionary"})
    add("enable times out", {"action": "enable", "id": 9, "enabled": True}, replies={"enable_dictionary Custom A": TimeoutError("No reply to enable_dictionary. Check the left half’s connection.")})
    add("hints auto", {"action": "hints", "mode": "auto"})
    add("hints off", {"action": "hints", "mode": "off"})
    for mode in ("on", "", None, 1):
        add(f"hints {mode!r}", {"action": "hints", "mode": mode})
    add("hints without a mode", {"action": "hints"})
    add("hints refused", {"action": "hints", "mode": "auto"}, replies={"stroke_hints auto": "ERR no"})
    add("layout", {"action": "layout", "layout": "dvorak"})
    add("layout not offered", {"action": "layout", "layout": "colemak"})
    add("layout not a string", {"action": "layout", "layout": 5})
    add("layout missing", {"action": "layout"})
    add("layout refused", {"action": "layout", "layout": "dvorak"}, replies={"set_host_layout dvorak": "ERR no"})
    add("layout list not JSON", {"action": "layout", "layout": "dvorak"}, replies={"get_parameter available_host_layouts": "nope"})

    # ---- set / remove / reset ----
    add("set an entry", {"action": "set", "id": 1, "outline": "KAT", "translation": "cat"})
    add("set a multi-stroke entry", {"action": "set", "id": 9, "outline": "KAT/TKOG", "translation": "cat dog"})
    add("set a long translation", {"action": "set", "id": 1, "outline": "KAT", "translation": "x" * 1300})
    add("set a unicode translation", {"action": "set", "id": 1, "outline": "KAT", "translation": "café 日本語 😀"})
    add("set an empty translation", {"action": "set", "id": 1, "outline": "KAT", "translation": ""})
    for translation in (5, None, ["a"], {"a": 1}, True, 1.5):
        add(f"set with translation {translation!r}", {"action": "set", "id": 1, "outline": "KAT", "translation": translation})
    add("set with a NUL in the translation", {"action": "set", "id": 1, "outline": "KAT", "translation": "a\0b"})
    add("set with a 2048 byte translation", {"action": "set", "id": 1, "outline": "KAT", "translation": "a" * 2048})
    add("set without a translation", {"action": "set", "id": 1, "outline": "KAT"})
    add("set without an outline", {"action": "set", "id": 1, "translation": "x"})
    for outline in ("", "KAT//TKOG", "ZZZ", "KAT/", "/KAT", 5, None, ["KAT"], "/".join(["KAT"] * 16), "/".join(["KAT"] * 17)):
        add(f"set with outline {outline!r}", {"action": "set", "id": 1, "outline": outline, "translation": "x"})
    add("remove an entry", {"action": "remove", "id": 1, "outline": "KAT"})
    add("remove refused", {"action": "remove", "id": 1, "outline": "KAT"}, replies={"native_remove_entry 1 KAT": "ERR Entry not found"})
    add("remove with a bad outline", {"action": "remove", "id": 1, "outline": "ZZZ"})
    add("reset a factory entry", {"action": "reset", "id": 2, "outline": "KAT"})
    add("reset on a non-factory dictionary", {"action": "reset", "id": 9, "outline": "KAT"})
    add("set: commit busy then ok", {"action": "set", "id": 1, "outline": "KAT", "translation": "cat"},
        replies={"native_entry commit": ["ERR Native store busy", "ERR Native store busy", "OK"]})
    add("set: commit busy forever", {"action": "set", "id": 1, "outline": "KAT", "translation": "cat"},
        replies={"native_entry commit": ["ERR Native store busy"] * 20}, costs={"native_entry commit": 40.0})
    add("set: commit busy, store unavailable", {"action": "set", "id": 1, "outline": "KAT", "translation": "cat"},
        replies={"native_entry commit": ["ERR Native store busy"] * 3, "user_dictionary_status": json.dumps({**STATUS, "read_only": True})})
    add("set: commit busy, store not ready", {"action": "set", "id": 1, "outline": "KAT", "translation": "cat"},
        replies={"native_entry commit": ["ERR Native store busy"] * 3, "user_dictionary_status": json.dumps({**STATUS, "ready": False})})
    add("set: commit busy, wrong format", {"action": "set", "id": 1, "outline": "KAT", "translation": "cat"},
        replies={"native_entry commit": ["ERR Native store busy"] * 3, "user_dictionary_status": json.dumps({**STATUS, "format": "X"})})
    add("set: commit busy, status is a list", {"action": "set", "id": 1, "outline": "KAT", "translation": "cat"},
        replies={"native_entry commit": ["ERR Native store busy"] * 3, "user_dictionary_status": "[1]"})
    steps = [json.dumps({**STATUS, "work_steps": n}) for n in range(12)]
    add("set: commit busy with progress resetting the stall timer", {"action": "set", "id": 1, "outline": "KAT", "translation": "cat"},
        replies={"native_entry commit": ["ERR Native store busy"] * 12 + ["OK"], "user_dictionary_status": steps + [json.dumps(STATUS)] * 8},
        costs={"native_entry commit": 20.0})
    add("set: commit busy with no progress stalls", {"action": "set", "id": 1, "outline": "KAT", "translation": "cat"},
        replies={"native_entry commit": ["ERR Native store busy"] * 30}, costs={"native_entry commit": 20.0})
    add("set: commit refused", {"action": "set", "id": 1, "outline": "KAT", "translation": "cat"}, replies={"native_entry commit": "ERR Entry rejected"})
    add("set: begin refused", {"action": "set", "id": 1, "outline": "KAT", "translation": "cat"}, replies={"native_entry begin 1 3 KAT": "ERR Too many entries"})
    add("set: data refused", {"action": "set", "id": 1, "outline": "KAT", "translation": "cat"}, replies={"native_entry data 0 Y2F0": "ERR bad data"})
    add("set: abort refused", {"action": "set", "id": 1, "outline": "KAT", "translation": "cat"}, replies={"native_entry abort": "ERR no"})
    add("set: begin times out", {"action": "set", "id": 1, "outline": "KAT", "translation": "cat"},
        replies={"native_entry begin 1 3 KAT": TimeoutError("No reply to native_entry. Check the left half’s connection.")})
    add("set: an unexpected failure is not wrapped", {"action": "set", "id": 1, "outline": "KAT", "translation": "cat"},
        replies={"native_entry begin 1 3 KAT": KeyError("odd")})
    add("set: commit busy then a non-busy error", {"action": "set", "id": 1, "outline": "KAT", "translation": "cat"},
        replies={"native_entry commit": ["ERR Native store busy", "ERR something else"]})
    add("set: busy reply with extra text", {"action": "set", "id": 1, "outline": "KAT", "translation": "cat"},
        replies={"native_entry commit": ["ERR Native store busy: maintenance", "OK"]})
    add("set: the second dwell", {"action": "set", "id": 1, "outline": "KAT", "translation": "cat"},
        replies={"native_entry commit": ["ERR Native store busy"] * 6 + ["OK"]}, costs={"native_entry commit": 0.5})
    add("set: no further status checks inside a second", {"action": "set", "id": 1, "outline": "KAT", "translation": "cat"},
        replies={"native_entry commit": ["ERR Native store busy"] * 6 + ["OK"]}, costs={"native_entry commit": 0.1})
    add("set: stall deadline hit exactly", {"action": "set", "id": 1, "outline": "KAT", "translation": "cat"},
        replies={"native_entry commit": ["ERR Native store busy"] * 4}, costs={"native_entry commit": 90.0})

    # ---- editor, export, backup ----
    add("open_editor", {"action": "open_editor", "id": 1})
    add("open_editor with reload and mode", {"action": "open_editor", "id": 1, "reload": True, "mode": "personal"})
    add("open_editor with reload not true", {"action": "open_editor", "id": 1, "reload": 1})
    add("open_editor on a read-only store", {"action": "open_editor", "id": 1}, replies={"user_dictionary_status": json.dumps({**STATUS, "read_only": True})})
    add("open_editor with a status list", {"action": "open_editor", "id": 1}, replies={"user_dictionary_status": "[1]"})
    add("open_editor refused", {"action": "open_editor", "id": 1}, returns={"read_document": ValueError("Too large to edit")})
    add("preview_editor", {"action": "preview_editor", "id": 1, "edits": [1]})
    add("apply_editor", {"action": "apply_editor", "id": 1, "edits": [1]})
    add("apply_editor refused", {"action": "apply_editor", "id": 1}, returns={"change_document": RuntimeError("Changed on the keyboard")})
    add("export", {"action": "export", "id": 9})
    add("export an empty dictionary", {"action": "export", "id": 9}, returns={"export_dictionary": {}})
    add("export refused", {"action": "export", "id": 9}, returns={"export_dictionary": ValueError("No installed dictionary named 'x'")})
    add("backup", {"action": "backup"})
    add("backup refused", {"action": "backup"}, returns={"backup_all": RuntimeError("Host layout changed during backup. Retry the backup.")})

    # ---- clear_personal ----
    add("clear_personal", {"action": "clear_personal", "id": 1, "confirm": "CLEAR"}, replies={"native_install status": "NATIVE2 READY 12"})
    for confirm in ("clear", "", None, "CLEAR "):
        add(f"clear_personal confirm {confirm!r}", {"action": "clear_personal", "id": 1, "confirm": confirm})
    add("clear_personal on another dictionary", {"action": "clear_personal", "id": 9, "confirm": "CLEAR"})
    add("clear_personal without a selection", {"action": "clear_personal", "confirm": "CLEAR"})
    add("clear_personal on firmware without the capability", {"action": "clear_personal", "id": 1, "confirm": "CLEAR"}, replies={UPDATE + "snapshot-clear-capabilities": "OK CLEAR2"})
    add("clear_personal returns nothing to wait for", {"action": "clear_personal", "id": 1, "confirm": "CLEAR"}, returns={"clear_personal": False})
    add("clear_personal returns an empty dict", {"action": "clear_personal", "id": 1, "confirm": "CLEAR"}, returns={"clear_personal": {}})

    # ---- packages ----
    add("preview_package", {"action": "preview_package", "package": "lapwing-base", "restore": True})
    add("preview_package without restore", {"action": "preview_package", "package": "lapwing-base"})
    add("preview_package with a string restore", {"action": "preview_package", "package": "lapwing-base", "restore": "yes"})
    add("apply_package", {"action": "apply_package", "package": "lapwing-base", "restore": False, "token": TOKEN})
    add("apply_package without a token", {"action": "apply_package", "package": "lapwing-base", "restore": False})
    add("apply_package with a short token", {"action": "apply_package", "package": "lapwing-base", "restore": False, "token": "abc"})
    add("apply_package with a long token", {"action": "apply_package", "package": "lapwing-base", "restore": False, "token": TOKEN + "a"})
    add("apply_package with a numeric token", {"action": "apply_package", "package": "lapwing-base", "restore": False, "token": 5})
    add("apply_package refused", {"action": "apply_package", "package": "lapwing-base", "restore": False, "token": TOKEN},
        returns={"factory_package": ValueError("Preview changed")})

    # ---- optimize ----
    for kind_id, operation in ((1, "personal"), (9, "convert"), (10, "consolidate")):
        add(f"preview_optimize {operation}", {"action": "preview_optimize", "id": kind_id, "operation": operation})
        add(f"apply_optimize {operation}", {"action": "apply_optimize", "id": kind_id, "operation": operation, "token": TOKEN})
    add("apply_optimize without a token", {"action": "apply_optimize", "id": 1, "operation": "personal"})
    add("apply_optimize with a short token", {"action": "apply_optimize", "id": 1, "operation": "personal", "token": "x"})
    for kind_id, operation in ((1, "convert"), (9, "personal"), (2, "personal"), (1, None), (10, "convert"), (9, "consolidate")):
        add(f"optimize the wrong operation {operation!r} on {kind_id}", {"action": "preview_optimize", "id": kind_id, "operation": operation})
    add("apply_optimize that publishes nothing", {"action": "apply_optimize", "id": 1, "operation": "personal", "token": TOKEN}, returns={"change_snapshot": False})

    # ---- merge ----
    add("preview_merge", {"action": "preview_merge", "upper": 9, "lower": 10})
    add("apply_merge", {"action": "apply_merge", "upper": 9, "lower": 10, "token": TOKEN})
    add("apply_merge without a token", {"action": "apply_merge", "upper": 9, "lower": 10})
    add("apply_merge refused", {"action": "apply_merge", "upper": 9, "lower": 10, "token": TOKEN}, returns={"merge_snapshots": ValueError("Dictionaries changed")})
    add("apply_offline_merge", {"action": "apply_offline_merge", "upper": 9, "lower": 10, "token": TOKEN})
    add("apply_offline_merge refused at capture", {"action": "apply_offline_merge"}, returns={"capture_approved_merge": ValueError("Preview first")})
    add("apply_offline_merge refused at install", {"action": "apply_offline_merge"}, returns={"install_offline_merge": RuntimeError("Write failed")})

    # ---- import ----
    source = json.dumps({"KAT": "cat", "TKOG": "dog"})
    add("preview_import", {"action": "preview_import", "name": "New Dictionary", "source": source})
    add("apply_import", {"action": "apply_import", "name": "New Dictionary", "source": source, "token": TOKEN})
    add("apply_import without a token", {"action": "apply_import", "name": "New Dictionary", "source": source})
    add("apply_import with a short token", {"action": "apply_import", "name": "New Dictionary", "source": source, "token": "x"})
    add("preview_import without a source", {"action": "preview_import", "name": "X"})
    add("preview_import without a name", {"action": "preview_import", "source": source})
    for name in ("", "a\nb", None, 5):
        add(f"preview_import with name {name!r}", {"action": "preview_import", "name": name, "source": source})
    add("preview_import of an empty dictionary", {"action": "preview_import", "name": "X", "source": "{}"})
    add("preview_import not an object", {"action": "preview_import", "name": "X", "source": "[]"})
    add("preview_import a bad outline", {"action": "preview_import", "name": "X", "source": '{"ZZZ": "x"}'})
    add("preview_import a duplicate key", {"action": "preview_import", "name": "X", "source": '{"KAT": "a", "KAT": "b"}'})
    add("preview_import a number translation", {"action": "preview_import", "name": "X", "source": '{"KAT": 5}'})
    add("import limit reached, merge possible", {"action": "preview_import", "name": "X", "source": source},
        returns={"named_import": ValueError("The keyboard has reached its dictionary limit")})
    add("import limit reached, merge impossible", {"action": "preview_import", "name": "X", "source": source},
        returns={"named_import": ValueError("The keyboard has reached its dictionary limit")}, replies={UPDATE + "snapshot-merge-capabilities": "ERR no"})
    add("import fails otherwise", {"action": "preview_import", "name": "X", "source": source}, returns={"named_import": ValueError("No space")})
    add("import fails with a different error type", {"action": "preview_import", "name": "X", "source": source}, returns={"named_import": RuntimeError("The keyboard has reached its dictionary limit")})
    for label, text in (("single quotes", "{'KAT': 'cat'}"), ("unterminated string", '{"KAT": "cat'),
                        ("missing colon", '{"KAT" "cat"}'), ("garbage after", '{"KAT": "cat"} x'), ("unterminated key", '{"KAT'), ("backslash at the end", '{"KAT": "c' + chr(92)), ("short unicode escape at a quote", '{"KAT": "' + chr(92) + 'u12"}'),
                        ("unicode escape at the end", '{"KAT": "' + chr(92) + 'u'), ("bad second surrogate", '{"KAT": "' + chr(92) + 'ud83d' + chr(92) + 'u12G4"}'),
                        ("nul in a string", '{"KAT": "a' + chr(0) + 'b"}'), ("unterminated after an escape", '{"KAT": "c' + chr(92) + 'n'),
                        ("empty", ""), ("only whitespace", "  \n "),
                        ("bad escape", '{"KAT": "c\\xat"}'), ("bad unicode escape", '{"KAT": "\\u12G4"}'), ("control character", '{"KAT": "c\tat"}'),
                        ("bad value", '{"KAT": nope}'), ("missing comma", '{"KAT": "a" "TKOG": "b"}'), ("unquoted key", '{KAT: "a"}'),
                        ("byte order mark", "﻿" + '{"KAT": "cat"}'), ("second line", '{\n"KAT": "cat"\n x}'), ("third line deep", '{\n"KAT": "cat",\n  "TKOG": x}'),
                        ("a bare minus", '{"KAT": -}'), ("trailing garbage on line 2", '{"KAT": "cat"}\n junk'), ("unterminated object", '{"KAT": "cat"'),
                        ("unterminated array", '{"KAT": ["a"'), ("missing value", '{"KAT": }'), ("a number with a leading zero", '{"KAT": 01}'),
                        ("nested bad", '{"KAT": {"a": }}'), ("tab indented", '{\n\t"KAT": x}'), ("crlf", '{\r\n"KAT": x}')):
        add(f"import_personal syntax error: {label}", {"action": "preview_import", "name": "X", "source": text})
        add(f"import syntax error: {label}", {"action": "import", "source": text})

    # ---- move / rename / delete / import / compact ----
    add("move", {"action": "move", "id": 9, "position": 3})
    for position in ("3", 3.7, True, None, "x", [1], "", " 4 ", -2):
        add(f"move to {position!r}", {"action": "move", "id": 9, "position": position})
    add("move without a position", {"action": "move", "id": 9})
    add("rename", {"action": "rename", "id": 9, "name": "Renamed"})
    add("rename without a name", {"action": "rename", "id": 9})
    add("rename to an unsafe name", {"action": "rename", "id": 9, "name": "a\nb"})
    add("rename the personal dictionary", {"action": "rename", "id": 1, "name": "x"})
    add("rename a base dictionary", {"action": "rename", "id": 2, "name": "x"})
    add("rename a snapshot", {"action": "rename", "id": 10, "name": "x"})
    add("delete", {"action": "delete", "id": 9, "confirm": "Custom A"})
    add("delete a snapshot", {"action": "delete", "id": 10, "confirm": "Snap One"})
    add("delete without confirming", {"action": "delete", "id": 9})
    add("delete with the wrong confirmation", {"action": "delete", "id": 9, "confirm": "custom a"})
    add("delete the personal dictionary", {"action": "delete", "id": 1, "confirm": "user_dictionary"})
    add("delete a base dictionary", {"action": "delete", "id": 2, "confirm": "Lapwing Base"})
    add("a delete that publishes nothing", {"action": "delete", "id": 9, "confirm": "Custom A"}, returns={"transfer": False})
    add("move on firmware without manage capability", {"action": "move", "id": 9, "position": 1}, replies={UPDATE + "manage-capabilities": "OK MANAGE0"})
    add("import a compact dictionary", {"action": "import", "source": source})
    add("import with a name", {"action": "import", "source": source, "name": "X"})
    add("import with an empty name", {"action": "import", "source": source, "name": ""})
    add("import without a source", {"action": "import"})
    add("import of an empty dictionary", {"action": "import", "source": "{}"})
    add("compact", {"action": "compact", "token": TOKEN})
    add("compact without a token", {"action": "compact"})
    add("compact with a short token", {"action": "compact", "token": "x"})
    add("compact that publishes nothing", {"action": "compact", "token": TOKEN}, returns={"transfer": False})
    add("compact refused", {"action": "compact", "token": TOKEN}, returns={"transfer": ValueError("Preview changed")})


    # ---- boundaries found by mutation testing ----
    setup = {"action": "setup", "approved": True, "serial": "AB12", "packages": ["lapwing-base", "plover"]}
    for status in ("NATIVE2 READY 0", "NATIVE2 READY 9", "NATIVE2 READY 90", "NATIVE2 UNPROVISIONED 9 extra words", "NATIVE2 UNPROVISIONED 10"):
        add(f"setup expecting {status!r}", {**setup, "expected_status": status, "confirm_serial": "AB12"},
            keyboard=Keyboard(IMAGE, statuses=[status, status, "NATIVE2 READY 12", "NATIVE2 READY 12", "NATIVE2 READY 12", "NATIVE2 READY 12"], layout=[LAYOUT_OK] * 4, pages=GOOD_PAGES))
    for target in (5, "", None):
        add(f"setup for serial {target!r} with no job serial", {**setup, "serial": target}, serial=None)
    for text in (NL + '{"KAT": x}', NL + NL + '{"KAT": x}', NL + '{"KAT": "a"}' + NL + "junk", '{"KAT": "a"}' + NL + "x"):
        add(f"import syntax error after leading newlines {text!r}", {"action": "import", "source": text})
    add("layout in the list but not a string", {"action": "layout", "layout": 5}, replies={"get_parameter available_host_layouts": '["qwerty", 5]'})
    add("layout in the list as a list", {"action": "layout", "layout": ["a"]}, replies={"get_parameter available_host_layouts": '["qwerty", ["a"]]'})
    add("layout null in the list", {"action": "layout", "layout": None}, replies={"get_parameter available_host_layouts": '["qwerty", null]'})
    for action, extra_fields in (("rename", {"name": "x"}), ("delete", {"confirm": "user_dictionary"})):
        add(f"{action} a named dictionary called user_dictionary", {"action": action, "id": 5, **extra_fields},
            replies={"native_catalog": json.dumps([{"id": 5, "name": "user_dictionary", "kind": "named"}])})
    for position in (False, "0", "9", "09", "+7", "-0"):
        add(f"move to {position!r} (int rules)", {"action": "move", "id": 9, "position": position})


    # ---- commit waiting: deadlines, status shapes (found by mutation testing) ----
    entry = {"action": "set", "id": 1, "outline": "KAT", "translation": "cat"}
    busy = ["ERR Native store busy"] * 45
    tail_ok = [json.dumps(STATUS)] * 8

    def statuses(count, **vary):
        return [json.dumps({**STATUS, **{key: value(i) for key, value in vary.items()}}) for i in range(count)] + tail_ok

    per_loop = 0.001 + 0.05  # a status read and the poll sleep between commits
    for label, target in (("just before", 3599.5), ("just after", 3600.5)):
        cost = (target - 39 * per_loop) / 40
        add(f"set: total deadline {label} the 40th check", entry,
            replies={"native_entry commit": list(busy), "user_dictionary_status": statuses(45, work_steps=lambda i: i)},
            costs={"native_entry commit": cost})
    add("set: stall deadline not reached with steady progress", entry,
        replies={"native_entry commit": list(busy), "user_dictionary_status": statuses(45, work_steps=lambda i: i)},
        costs={"native_entry commit": 179.5})
    add("set: stall deadline passed on the first check", entry,
        replies={"native_entry commit": list(busy), "user_dictionary_status": statuses(45, work_steps=lambda i: i)},
        costs={"native_entry commit": 180.5})
    add("set: stall deadline hit exactly on the first check", entry,
        replies={"native_entry commit": list(busy), "user_dictionary_status": statuses(45, work_steps=lambda i: i)},
        costs={"native_entry commit": 180.0})
    for key in ("work_steps", "generation", "tail"):
        add(f"set: only {key} changes between checks", entry,
            replies={"native_entry commit": list(busy), "user_dictionary_status": statuses(45, **{key: lambda i: 100 + i})},
            costs={"native_entry commit": 100.0})
    add("set: work_steps alternates between zero and absent", entry,
        replies={"native_entry commit": list(busy), "user_dictionary_status": [json.dumps({**STATUS, "work_steps": 0}), json.dumps(STATUS)] * 25 + tail_ok},
        costs={"native_entry commit": 100.0})
    add("set: status never changes", entry,
        replies={"native_entry commit": list(busy), "user_dictionary_status": [json.dumps(STATUS)] * 45 + tail_ok},
        costs={"native_entry commit": 100.0})
    for status in ("null", "5", '"x"', "true", "{}", "[]", "1.5"):
        add(f"set: commit busy, status is {status}", entry,
            replies={"native_entry commit": ["ERR Native store busy"] * 3, "user_dictionary_status": status})
    add("set: commit busy, status is not JSON", entry,
        replies={"native_entry commit": ["ERR Native store busy"] * 3, "user_dictionary_status": "garbage"})
    add("set: commit busy, status is empty", entry,
        replies={"native_entry commit": ["ERR Native store busy"] * 3, "user_dictionary_status": ""})
    add("refresh with a deferred layout scan", {"action": "refresh"},
        keyboard=Keyboard(IMAGE, layout=["ERR Layout scan deferred until the flash and input are idle", LAYOUT_OK], pages=GOOD_PAGES))
    add("refresh with a layout scan deferred twice", {"action": "refresh"},
        keyboard=Keyboard(IMAGE, layout=["ERR Layout scan deferred by input"] * 2 + [LAYOUT_OK], pages=GOOD_PAGES))

    # ---- repack ----
    add("apply_repack", {"action": "apply_repack", "token": TOKEN})
    add("restore_repack_backup", {"action": "restore_repack_backup", "token": TOKEN, "confirm": "AB12"})
    add("apply_repack refused at capture", {"action": "apply_repack"}, returns={"capture_approved_repack": ValueError("Preview first")})
    add("apply_repack refused at install", {"action": "apply_repack"}, returns={"install_repack": RuntimeError("Write failed")})
    add("restore refused", {"action": "restore_repack_backup"}, returns={"restore_original": ValueError("Confirm the USB serial")})
    add("apply_repack with another serial", {"action": "apply_repack"}, serial="ZZ99")

    # ---- setup ----
    setup = {"action": "setup", "approved": True, "serial": "AB12", "packages": ["lapwing-base", "plover"]}
    unprovisioned = lambda: Keyboard(IMAGE, statuses=[UNPROVISIONED, UNPROVISIONED, "NATIVE2 READY 12", "NATIVE2 READY 12", "NATIVE2 READY 12", "NATIVE2 READY 12"], layout=[LAYOUT_OK] * 4, pages=GOOD_PAGES)
    add("setup", setup, keyboard=unprovisioned())
    add("setup over Bluetooth", setup, transport="bluetooth", keyboard=unprovisioned())
    add("setup not approved", {**setup, "approved": False})
    add("setup approved is truthy but not true", {**setup, "approved": 1})
    add("setup without approval", {k: v for k, v in setup.items() if k != "approved"})
    for target in ("", None, 5, "ZZ99"):
        add(f"setup for serial {target!r}", {**setup, "serial": target})
    add("setup with no job serial", setup, serial=None, keyboard=unprovisioned())
    add("setup without packages", {k: v for k, v in setup.items() if k != "packages"}, keyboard=unprovisioned())
    for packages in ([], "lapwing-base", ["nope"], ["lapwing-base", "lapwing-base"], [5], None):
        add(f"setup with packages {packages!r}", {**setup, "packages": packages}, keyboard=unprovisioned())
    add("setup with an unusual expected status", {**setup, "expected_status": "NATIVE2 READY 12", "confirm_serial": "AB12"},
        keyboard=Keyboard(IMAGE, statuses=["NATIVE2 READY 12", "NATIVE2 READY 12", "NATIVE2 READY 12", "NATIVE2 READY 12", "NATIVE2 READY 12"], layout=[LAYOUT_OK] * 4, pages=GOOD_PAGES))
    add("setup replacing a pool without confirming", {**setup, "expected_status": "NATIVE2 READY 12"})
    add("setup replacing a pool with the wrong confirmation", {**setup, "expected_status": "NATIVE2 READY 12", "confirm_serial": "ZZ99"})
    for status in (None, 5, "garbage", "NATIVE2 INSTALL 3", ""):
        add(f"setup with expected status {status!r}", {**setup, "expected_status": status})
    add("setup with a status that changed", setup, keyboard=Keyboard(IMAGE, statuses=["NATIVE2 UNPROVISIONED 7"]))
    add("setup on firmware without factory members", setup, keyboard=unprovisioned(), replies={FACTORY: "OK FACTORY1"})
    add("setup provisioning fails", setup, keyboard=unprovisioned(), returns={"provision": RuntimeError("Write failed")})
    add("setup image preparation fails", setup, keyboard=unprovisioned(), returns={"prepare_install_image": ValueError("Missing sources")})
    add("setup keyboard never returns", setup, keyboard=Keyboard(IMAGE, statuses=[UNPROVISIONED, UNPROVISIONED] + ["NATIVE2 INSTALL 3"] * 200), costs={"native_install status": 1.0})
    add("setup keyboard returns after being busy", setup, keyboard=Keyboard(IMAGE, statuses=[UNPROVISIONED, UNPROVISIONED, "ERR Native flash busy", "ERR Native flash busy", "NATIVE2 READY 12", "NATIVE2 READY 12", "NATIVE2 READY 12", "NATIVE2 READY 12"], layout=[LAYOUT_OK] * 4, pages=GOOD_PAGES))
    add("setup keyboard busy past the long deadline", setup, keyboard=Keyboard(IMAGE, statuses=[UNPROVISIONED, UNPROVISIONED] + ["ERR Native flash busy"] * 300), costs={"native_install status": 1.0})
    add("setup keyboard reopens slowly", setup, keyboard=unprovisioned(), opens=[None, None, RuntimeError("The selected keyboard identity differs. Connect the intended Steno Squid left half."), OSError("gone"), ValueError("odd")])
    add("setup reopen fails with an unexpected error", setup, keyboard=unprovisioned(), opens=[None, None, KeyError("odd")])
    add("setup load_view fails and retries", setup, keyboard=Keyboard(IMAGE, statuses=[UNPROVISIONED, UNPROVISIONED, "NATIVE2 READY 12", "NATIVE2 READY 12"], layout=[LAYOUT_OK] * 4, pages=GOOD_PAGES), replies={"native_catalog": ["[]"] + [json.dumps(ROWS)] * 6})

    # ---- publishing and waiting for the keyboard ----
    pub = {"action": "compact", "token": TOKEN}
    add("published, keyboard back at once", pub, keyboard=Keyboard(IMAGE, statuses=["NATIVE2 READY 12"] * 8, layout=[LAYOUT_OK] * 4, pages=GOOD_PAGES))
    add("published, keyboard checking flash first", pub, keyboard=Keyboard(IMAGE, statuses=["NATIVE2 READY 12", "ERR Native flash busy", "ERR Native flash busy", "NATIVE2 READY 12", "NATIVE2 READY 12", "NATIVE2 READY 12", "NATIVE2 READY 12"], layout=[LAYOUT_OK] * 4, pages=GOOD_PAGES))
    add("published, keyboard never returns", pub, keyboard=Keyboard(IMAGE, statuses=["NATIVE2 READY 12"] + ["NATIVE2 BOGUS"] * 200), costs={"native_install status": 1.0})
    add("published, keyboard busy until the long deadline", pub, keyboard=Keyboard(IMAGE, statuses=["NATIVE2 READY 12"] + ["ERR Native flash busy"] * 300), costs={"native_install status": 1.0})
    add("published, reopen fails then works", pub, keyboard=Keyboard(IMAGE, statuses=["NATIVE2 READY 12"] * 8, layout=[LAYOUT_OK] * 4, pages=GOOD_PAGES),
        opens=[None, RuntimeError("The selected keyboard identity differs. Connect the intended Steno Squid left half."), TimeoutError("x"), None])
    add("published, load view fails with an unexpected error", pub, keyboard=Keyboard(IMAGE, statuses=["NATIVE2 READY 12"] * 8, layout=[LAYOUT_OK] * 4, pages=GOOD_PAGES),
        replies={"native_catalog": [json.dumps(ROWS), KeyError("odd")]})
    add("published, status reply raises a timeout", pub, keyboard=Keyboard(IMAGE, statuses=["NATIVE2 READY 12", TimeoutError("No reply"), "NATIVE2 READY 12", "NATIVE2 READY 12", "NATIVE2 READY 12", "NATIVE2 READY 12"], layout=[LAYOUT_OK] * 4, pages=GOOD_PAGES))
    add("the final view fails", {"action": "enable", "id": 9, "enabled": True}, replies={"native_catalog": [json.dumps(ROWS), "[]"]})
    return {"schema": 1, "cases": cases, "direct": direct_cases()}


def direct_case(title, source, *, identity=1, replies=None, costs=None):
    """native_manage.import_entries on its own (the Manager only ever sends one entry, but the progress lines are for many)."""
    from import_personal import parse_named
    world = World(replies, costs=costs, keyboard=None)
    undo = install(world)
    entries, names = parse_named(source)
    try:
        def progress(message):
            world.events.append({"e": "progress", "m": message})
        console = WorldConsole(world)
        try:
            result = {"value": native_manage.import_entries(console, identity, entries, names, progress)}
        except Exception as error:
            result = {"error": err(error)}
    finally:
        undo()
    return {"title": title, "kind": "import_entries", "identity": identity, "source": source, "expect": result, "events": world.events}


def direct_cases():
    cases = []
    few = json.dumps({"KAT": "cat", "TKOG": "dog", "PWAOEUT": "bite"})
    from golden_decode import dictionary
    many = lambda count, text: json.dumps(dictionary(11, count))
    cases.append(direct_case("three entries", few))
    cases.append(direct_case("one long entry in several chunks", json.dumps({"KAT": "x" * 1300})))
    cases.append(direct_case("an entry of exactly 512 bytes", json.dumps({"KAT": "y" * 512})))
    cases.append(direct_case("an entry of 513 bytes", json.dumps({"KAT": "y" * 513})))
    cases.append(direct_case("an empty entry", json.dumps({"KAT": ""})))
    cases.append(direct_case("a multibyte entry", json.dumps({"KAT": "日本語" * 30})))
    cases.append(direct_case("99 entries", many(99, "w")))
    cases.append(direct_case("100 entries", many(100, "w")))
    cases.append(direct_case("101 entries", many(101, "w")))
    cases.append(direct_case("205 entries", many(205, "w")))
    cases.append(direct_case("a failure on the 2nd entry", few, replies={"native_entry commit": ["OK", "ERR Rejected", "OK"]}))
    cases.append(direct_case("a failure on the 150th entry", many(150, "w"), replies={"native_entry commit": ["OK"] * 149 + ["ERR Rejected"]}))
    cases.append(direct_case("another dictionary id", few, identity=4000000000))
    cases.append(direct_case("abort refused", few, replies={"native_entry abort": "ERR no"}))
    return cases


def main():
    text = json.dumps(generate(), indent=0) + chr(10)
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8").replace(chr(13) + chr(10), chr(10)) != text:
            sys.exit("golden/jobs_vectors.json is stale; rerun golden_jobs.py")
        print("jobs vectors are current")
        return
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline=chr(10))
    print(f"wrote {OUT} ({len(text) // 1024} KiB, {len(json.loads(text)['cases'])} cases)")


if __name__ == "__main__":
    main()
