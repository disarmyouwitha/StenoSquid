"""Golden vectors for repack_jobs.js and recover.js (manager_repack.py and native_recovery.recover_usb over a scripted keyboard).

The oracle runs the real Python functions against a keyboard simulator that serves the layout map, image metadata, journal usage,
flash reads, status and the recovery write commands, with the recovery folder replaced by a temporary directory and the clock,
the capture time and the plan token made deterministic. Each scenario records the console transcript, the progress messages, the
results (or the exact failure) and a SHA-256 of every recovery file Python saved, so the JavaScript must save identical bytes.
"""
import contextlib
import datetime
import hashlib
import json
from pathlib import Path
import random
import sys
import tempfile
from types import SimpleNamespace

import golden_decode as images  # also puts the tools/ directories on sys.path
import golden_snapshot as base_vectors
import golden_snapshot_ops as ops_vectors
import golden_state as gs
import manager_repack
import native_install
import native_recovery as recovery
import native_repack as repack
import native_snapshot
from build_native_image import SMALL_POOL_END
from golden_layout import layer
from native_update import row_name

OUT = Path(__file__).with_name("golden") / "repack_flow_vectors.json"
SERIAL = "AB12"
TOKEN = "00112233445566778899aabbccddeeff"
IDENTITY = hashlib.sha256(SERIAL.encode()).hexdigest()
STAMP = "2026-01-02T03:04:05+00:00"


class Clock:
    def __init__(self):
        self.t, self.sleeps = 0.0, []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds

    def monotonic(self):
        return self.t


class Fixed(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=tz)


class Keyboard(gs.Keyboard):
    """The state keyboard plus the recovery write commands; `installed` flips when the image is published."""

    def __init__(self, image, rows, *, overrides=None, finish_reply="OK", **options):
        super().__init__(image, **options)
        self.installed, self.rows, self.overrides, self.finish_reply = False, rows, overrides or {}, finish_reply

    def handle(self, text):
        for prefix, value in self.overrides.items():
            if text.startswith(prefix):
                if isinstance(value, list):
                    return value.pop(0) if value else None
                return value
        if text == "native_install geometry":
            return "OK POOL1 255"
        if text.startswith("native_install match RECOVERY "):
            return "OK MATCH" if self.installed else "ERR Recovery contents differ"
        if text.startswith(("native_install begin ", "native_install erase ", "native_install write ", "set_host_layout ")):
            return "OK"
        if text == "native_install abort":
            return "OK"
        if text == "native_install finish":
            self.installed = True
            return self.finish_reply
        if text == "list_dictionaries":
            return "[]"
        if text == "native_catalog" and self.installed:
            return json.dumps(self.rows)
        return super().handle(text)


def layout_reply(spread):
    data = ["c", "c"] + ["."] * (SMALL_POOL_END - 2) + ["x"]
    for index in range(2, SMALL_POOL_END):
        if spread and index % 4:
            data[index] = "b"
    text = "".join(data)
    return f"OK MAP1 1 w vv {text} {text}"


def rows_of(bundle):
    _, snapshot = recovery.reconstruct(bundle)
    return [{"id": int.from_bytes(row[:4], "little"), "name": row_name(row), "enabled": bool(row[4])} for row in snapshot.rows]


def failure(error):
    return {"type": type(error).__name__, "message": str(error)}


def save_atomic(path, document, dump=json.dump):
    """What the page's bridge does: refuse to replace a file, write the compact UTF-8 JSON once."""
    path = Path(path)
    if path.exists():
        raise FileExistsError(path)
    path.write_bytes((json.dumps(document, ensure_ascii=False, separators=(",", ":")) + chr(10)).encode("utf-8"))


def read_bundle(path, *, durable=False):
    path = Path(path).resolve(strict=True)
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


@contextlib.contextmanager
def patched(console, directory):
    clock = Clock()
    saved = (native_install.open_console, native_install.time, manager_repack.backup_directory, manager_repack.secrets,
             datetime.datetime, manager_repack.save_atomic, recovery.save_atomic, repack.read_bundle, manager_repack.read_bundle)
    manager_repack.save_atomic = recovery.save_atomic = save_atomic
    manager_repack.read_bundle = repack.read_bundle = read_bundle

    @contextlib.contextmanager
    def open_console(serial=None):
        yield console

    native_install.open_console, native_install.time = open_console, clock
    manager_repack.backup_directory = lambda serial: Path(directory) / hashlib.sha256(serial.encode()).hexdigest()
    manager_repack.secrets = SimpleNamespace(token_hex=lambda n: TOKEN)
    datetime.datetime = Fixed
    try:
        yield clock
    finally:
        (native_install.open_console, native_install.time, manager_repack.backup_directory, manager_repack.secrets,
         datetime.datetime, manager_repack.save_atomic, recovery.save_atomic, repack.read_bundle, manager_repack.read_bundle) = saved


def files_in(directory):
    return {str(path.relative_to(directory)).replace(chr(92), "/"): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(Path(directory).rglob("*.json"))}


def texts_in(directory):
    return {str(path.relative_to(directory)).replace(chr(92), "/"): path.read_bytes().decode("utf-8") for path in sorted(Path(directory).rglob("*.json"))}


def scenario(title, image, ops, *, layout=None, overrides=None, statuses=None, preload=None, extra=None, rows=None, pages=True, replies=None, finish_reply="OK"):
    """Run `ops` in order on one keyboard. Each op is (name, argument)."""
    identities, journals = gs.fixture_facts(image)
    keyboard = Keyboard(image, rows or [], overrides=overrides, finish_reply=finish_reply, statuses=statuses, layout=layout or [layout_reply(True)] * 4,
                        pages=gs.usage_pages(identities, journals) if pages else {})
    base = gs.base_replies(**{"get_parameter host_layout": "Mac U.S.", **(replies or {})})
    console = gs.Transcript(base, serial=SERIAL, handler=keyboard.handle)
    results, progress, state = [], [], {}
    with tempfile.TemporaryDirectory() as directory:
        for relative, text in (preload or {}).items():
            path = Path(directory) / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode("utf-8"))
        with patched(console, directory) as clock:
            try:
                for name, argument in ops:
                    if name == "preview":
                        value = manager_repack.preview_repack(console, progress.append)
                    elif name == "capture":
                        source, plan = manager_repack.capture_approved_repack(console, argument, progress.append)
                        state["source"], state["plan"] = source, plan
                        value = {"plan": plan, "source_sha256": source["sha256"]}
                    elif name == "install":
                        manager_repack.install_repack(state["source"], state["plan"], progress.append)
                        value = None
                    elif name == "restore":
                        manager_repack.restore_original(SERIAL, argument, progress.append)
                        value = None
                    elif name == "capture_merge":
                        source, original = manager_repack.capture_approved_merge(console, argument, progress.append)
                        state["source"], state["original"] = source, original
                        value = {"source_sha256": source["sha256"]}
                    elif name == "install_merge":
                        value = manager_repack.install_offline_merge(state["source"], state["original"], argument, progress.append)
                    elif name == "last":
                        path = manager_repack.last_repack_backup(SERIAL)
                        value = None if path is None else path.name
                    else:
                        raise AssertionError(name)
                    results.append(json.loads(json.dumps(value)))
                outcome = {"results": results}
            except Exception as error:
                outcome = {"error": failure(error), "results": results}
            files = files_in(Path(directory))
            sleeps = list(clock.sleeps)
    return {"title": title, "ops": [[name, argument] for name, argument in ops], "expect": outcome, "progress": progress, "sleeps": sleeps,
            "files": files, "preload": preload or {}, "transcript": gs.pack_transcript(console)}, outcome, files


def generate():
    base_image, snap_image, ids = ops_vectors.build_images()
    cases = []

    def run(title, image, ops, **options):
        record, outcome, files = scenario(title, image, ops, **options)
        cases.append(record)
        return outcome, files

    # A keyboard whose repack previews as an improvement, and the bundle the install will write.
    source = recovery.capture(lambda a, n: bytes(base_image[a:a + n]), base_vectors_probe(base_image), "Mac U.S.")
    _, built, _ = repack.build_repack(source, [])
    rows = rows_of(built)

    preview, _ = run("preview a repack that helps", base_image, [("preview", None)])
    token = preview["results"][0].get("token")
    run("preview a repack that does not help", base_image, [("preview", None)], layout=[layout_reply(False)] * 2)
    run("preview without repack support", base_image, [("preview", None)], replies={gs.UPDATE + "repack-capabilities": "OK"})
    run("preview without recovery support", base_image, [("preview", None)], replies={gs.UPDATE + "recovery-capabilities": "OK"})
    run("preview while the layout is read-only", base_image, [("preview", None)], layout=[layout_reply(True).replace(" w vv", " r vv")] * 2)
    run("preview when the flash changes", base_image, [("preview", None)], statuses=["NATIVE2 READY 13"])
    run("preview when the generation changes", base_image, [("preview", None)],
        overrides={"user_dictionary_status": [json.dumps(gs.STATUS), json.dumps({**gs.STATUS, "generation": 13})]})

    approved = {"serial": SERIAL, "confirm": SERIAL, "token": token}
    run("capture an approved repack", base_image, [("capture", approved)])
    run("capture with the wrong confirmation", base_image, [("capture", {**approved, "confirm": "ZZ"})])
    run("capture with the wrong serial", base_image, [("capture", {**approved, "serial": "ZZ"})])
    run("capture with a stale token", base_image, [("capture", {**approved, "token": "0" * 32})])
    run("capture when the flash changes during the backup", base_image, [("capture", approved)],
        overrides={"user_dictionary_status": [json.dumps(gs.STATUS)] * 2 + [json.dumps({**gs.STATUS, "tail": 9})]})

    run("capture and install a repack", base_image, [("capture", approved), ("install", None), ("last", None)], rows=rows)
    run("install when the live pool changed", base_image, [("capture", approved), ("install", None)], rows=rows,
        statuses=["NATIVE2 READY 12"] * 3 + ["NATIVE2 READY 13"])
    run("install when finish is not acknowledged but publishes", base_image, [("capture", approved), ("install", None)], rows=rows, finish_reply="ERR Native busy")
    run("install when a write is refused", base_image, [("capture", approved), ("install", None)], rows=rows,
        overrides={"native_install write 5": "ERR flash"})
    run("install when the recovered catalog is wrong", base_image, [("capture", approved), ("install", None)], rows=[])
    run("install when the pool never matches", base_image, [("capture", approved), ("install", None)], rows=rows,
        overrides={"native_install match RECOVERY ": ["ERR Recovery contents differ"] * 4})
    run("install when the match check is unavailable", base_image, [("capture", approved), ("install", None)], rows=rows,
        overrides={"native_install match RECOVERY ": "ERR busy"})
    run("install when the live pool lost repack support", base_image, [("capture", approved), ("install", None)], rows=rows,
        overrides={"native_install update repack-capabilities": ["OK REPACK1", "ERR no"]})

    # Restore the original backup that the install above saved.
    seed = scenario_texts(base_image, approved, rows)
    restore_request = {"serial": SERIAL, "confirm": SERIAL, "token": TOKEN}
    unprovisioned = ["NATIVE2 UNPROVISIONED 0"]
    run("restore the original backup", base_image, [("restore", restore_request), ("last", None)], preload=seed, statuses=unprovisioned, rows=rows_of(source))
    run("restore with the wrong confirmation", base_image, [("restore", {**restore_request, "confirm": "ZZ"})], preload=seed, statuses=unprovisioned)
    run("restore with a malformed token", base_image, [("restore", {**restore_request, "token": "xyz"})], preload=seed, statuses=unprovisioned)
    run("restore with an unknown token", base_image, [("restore", {**restore_request, "token": "f" * 32})], preload=seed, statuses=unprovisioned)
    run("restore with no saved backup", base_image, [("restore", restore_request)], statuses=unprovisioned)
    run("restore onto a readable pool", base_image, [("restore", restore_request)], preload=seed, rows=rows_of(source))
    other = {name: text.replace('"AB12"', '"CD34"') for name, text in seed.items()}
    run("restore a backup of another keyboard", base_image, [("restore", restore_request)], preload=other, statuses=unprovisioned)
    marked = {name: text for name, text in seed.items()}
    marker = [name for name in marked if name.endswith("attempt.json")][0]
    marked[marker] = json.dumps({"original_sha256": "0" * 64, "optimized_file_sha256": "0" * 64}) + chr(10)
    run("restore with a changed marker", base_image, [("restore", restore_request)], preload=marked, statuses=unprovisioned)

    # Offline merge of two adjacent compact dictionaries.
    merge_source = recovery.capture(lambda a, n: bytes(snap_image[a:a + n]), base_vectors_probe(snap_image), "Mac U.S.")
    _, merged_original = None, recovery.reconstruct(merge_source)
    upper, lower = ids["Snap Two"], ids["Snap One"]
    merge_token = native_snapshot.offline_merge_token(merged_original[1], SERIAL, upper, lower)
    merged_bundle, _ = repack.build_merge_repack(merge_source, merged_original, upper, lower)
    merge_request = {"serial": SERIAL, "confirm": SERIAL, "token": merge_token, "upper": upper, "lower": lower}
    merge_rows = rows_of(merged_bundle)
    run("capture an approved merge", snap_image, [("capture_merge", merge_request)])
    run("merge with a stale token", snap_image, [("capture_merge", {**merge_request, "token": "0" * 64})])
    run("merge with a malformed token", snap_image, [("capture_merge", {**merge_request, "token": "xyz"})])
    run("merge with a wrong confirmation", snap_image, [("capture_merge", {**merge_request, "confirm": "ZZ"})])
    run("merge with a bad identity", snap_image, [("capture_merge", {**merge_request, "upper": "a"})])
    run("merge without repack support", snap_image, [("capture_merge", merge_request)], replies={gs.UPDATE + "repack-capabilities": "OK"})
    run("capture, build and install a merge", snap_image, [("capture_merge", merge_request), ("install_merge", merge_request)], rows=merge_rows)
    return {"schema": 1, "serial": SERIAL, "identity": IDENTITY, "token": TOKEN, "stamp": STAMP, "cases": cases}


def base_vectors_probe(image):
    from native_update import Snapshot
    address, size, crc = base_vectors.catalog_of(image)
    return Snapshot(lambda a, n: bytes(image[a:a + n]), address, size, crc, metadata_only=True, pool_end=SMALL_POOL_END)


def scenario_texts(image, approved, rows):
    """The text of every file the install saves (so restore scenarios can start from a folder that already holds them)."""
    identities, journals = gs.fixture_facts(image)
    keyboard = Keyboard(image, rows, layout=[layout_reply(True)] * 4, pages=gs.usage_pages(identities, journals))
    console = gs.Transcript(gs.base_replies(**{"get_parameter host_layout": "Mac U.S."}), serial=SERIAL, handler=keyboard.handle)
    with tempfile.TemporaryDirectory() as directory:
        with patched(console, directory):
            source, plan = manager_repack.capture_approved_repack(console, approved, lambda message: None)
            manager_repack.install_repack(source, plan, lambda message: None)
        return texts_in(Path(directory))


def main():
    text = json.dumps(generate(), indent=0) + chr(10)
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8").replace(chr(13) + chr(10), chr(10)) != text:
            sys.exit("golden/repack_flow_vectors.json is stale; rerun golden_repack_flow.py")
        print("repack flow vectors are current")
        return
    OUT.write_text(text, encoding="utf-8", newline=chr(10))
    print(f"wrote {OUT} ({len(text) // 1024} KiB, {len(json.loads(text)['cases'])} scenarios)")


if __name__ == "__main__":
    main()
