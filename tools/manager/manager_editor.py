"""Guarded JSON editing of one effective dictionary and its saved overrides."""
import hashlib
import json
from functools import lru_cache
import struct

from import_personal import parse_named, unique_pairs
from native_manage import checked, import_entries
from native_update import export_dictionary, read_snapshot, row_name, update_command
from build_native_image import FACTORY_KEYS
from hybrid import format_stroke
from manifest import ROOT


def parse_source(source):
    if not isinstance(source, str):
        raise ValueError("Enter a JSON dictionary object.")
    try:
        return parse_named(source)
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON at line {error.lineno}, column {error.colno}: {error.msg}") from error


def parse_edits(source):
    if not isinstance(source, str):
        raise ValueError("Enter a JSON object of saved edits.")
    try:
        data = json.loads(source, object_pairs_hook=unique_pairs)
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON at line {error.lineno}, column {error.colno}: {error.msg}") from error
    if not isinstance(data, dict):
        raise ValueError("Saved edits must be a JSON object mapping outlines to translations or null.")
    entries, names = {}, {}
    for outline, text in data.items():
        parsed, labels = parse_source(json.dumps({outline: "" if text is None else text}))
        strokes = next(iter(parsed))
        value = None if text is None else parsed[strokes]
        if strokes in entries and entries[strokes] != value:
            raise ValueError("Two JSON keys describe the same outline with different edits.")
        entries[strokes] = value
        names.setdefault(strokes, labels[strokes])
    return entries, names


def editor_mode(mode):
    if mode not in {"all", "edits"}:
        raise ValueError("Choose the whole dictionary or saved edits.")
    return mode


def fingerprint(selected, serial, entries, mode="all"):
    rows = [(list(strokes), None if text is None else text.decode("utf-8")) for strokes, text in sorted(entries.items())]
    return hashlib.sha256(json.dumps([serial, selected["id"], selected["name"], selected["kind"], mode, rows],
                                     ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


@lru_cache(maxsize=6)
def factory_source(identity, name, digest):
    source = next((source for expected, label, _, source in FACTORY_KEYS.values()
                   if (expected, label) == (identity, name)), None)
    if not source or not digest:
        return None
    path = ROOT / "build" / source
    if not path.is_file():
        return None
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        return None
    entries, _ = parse_source(raw.decode("utf-8"))
    return {"/".join(map(format_stroke, strokes)): text.decode("utf-8") for strokes, text in entries.items()}


_documents = {}


def clear_documents(serial):
    for key in list(_documents):
        if key[0] == serial:
            del _documents[key]


def effective_document(console, selected, on_read, progress, *, on_snapshot=None):
    baseline = factory_source(selected["id"], selected["name"], selected.get("source_sha256")) if selected["kind"] == "base" else None
    if baseline is None:
        progress("Reading the dictionary from the keyboard…")
        return export_dictionary(console, selected["name"], on_read=on_read, on_snapshot=on_snapshot, trim_journals=True)
    progress("Opening bundled factory JSON; reading saved changes from the keyboard…")
    answer = update_command(console, "begin backup")
    try:
        snapshot, _ = read_snapshot(console, answer, on_read=on_read, trim_journals=True)
        row = next((row for row in snapshot.rows if struct.unpack_from("<I", row)[0] == selected["id"]), None)
        if (row is None or row_name(row) != selected["name"] or row[11] != 0
                or struct.unpack_from("<H", row, 8)[0] == 65535
                or row[64:96].hex() != selected["source_sha256"]):
            raise ValueError("The factory dictionary changed. Reconnect and reopen the editor.")
        if on_snapshot is not None:
            on_snapshot(snapshot)
        result = dict(baseline)
        for entry in snapshot.entries.values():
            if entry.identity != selected["id"]:
                continue
            outline = "/".join(map(format_stroke, entry.strokes))
            if entry.operation == 1:
                result.pop(outline, None)
            elif entry.operation == 0:
                result[outline] = entry.text.decode("utf-8")
        return result
    finally:
        update_command(console, "abort", timeout=5)


def snapshot_edits(snapshot, selected):
    row = next((row for row in snapshot.rows if struct.unpack_from("<I", row)[0] == selected["id"]), None)
    if row is None or row_name(row) != selected["name"] or row[11] == 2:
        raise ValueError("The dictionary changed. Reconnect and reopen the editor.")
    return {"/".join(map(format_stroke, entry.strokes)): None if entry.operation == 1 else entry.text.decode("utf-8")
            for entry in snapshot.entries.values() if entry.identity == selected["id"] and entry.operation in (0, 1)}


def saved_edits(console, selected, on_read, progress, *, on_snapshot=None):
    progress("Reading saved edits from the keyboard…")
    answer = update_command(console, "begin backup")
    try:
        snapshot, _ = read_snapshot(console, answer, on_read=on_read, trim_journals=True)
        result = snapshot_edits(snapshot, selected)
        if on_snapshot is not None:
            on_snapshot(snapshot)
        return result
    finally:
        update_command(console, "abort", timeout=5)


def make_document(console, selected, entries, mode):
    source = json.dumps(entries, ensure_ascii=False, indent=2) + "\n"
    parsed, _ = (parse_edits if mode == "edits" else parse_source)(source)
    return dict(id=selected["id"], name=selected["name"], kind=selected["kind"], source=source,
                count=len(parsed), mode=mode, version=fingerprint(selected, console.serial, parsed, mode))


def read_document(console, selected, progress=lambda _: None, *, force=False, mode="all"):
    mode = editor_mode(mode)
    status = json.loads(console.command("user_dictionary_status"))
    key = None
    if status.get("ready") and type(status.get("generation")) is int and type(status.get("tail")) is int:
        key = (console.serial, selected["id"], selected["name"], selected["kind"], selected.get("source_sha256"), selected.get("image_crc"), selected.get("image_bytes"),
               status["generation"], status["tail"], mode)
        if not force and key in _documents:
            return dict(_documents[key])
    received, reported = 0, -1

    def on_read(count):
        nonlocal received, reported
        received += count
        bucket = received // 8192
        if bucket != reported:
            reported = bucket
            progress(f"Reading keyboard data: {received / 1024:,.1f} KiB…")

    related = {}

    def capture(snapshot):
        edits = snapshot_edits(snapshot, selected)
        related["edits"] = edits
        if mode == "all":
            return
        row = next(row for row in snapshot.rows if struct.unpack_from("<I", row)[0] == selected["id"])
        baseline = None
        if row[11] == 0 and struct.unpack_from("<H", row, 8)[0] == 65535:
            baseline = {}
        elif selected["kind"] == "base" and row[64:96].hex() == selected.get("source_sha256"):
            baseline = factory_source(selected["id"], selected["name"], selected.get("source_sha256"))
        if baseline is not None:
            complete = dict(baseline)
            for outline, text in edits.items():
                if text is None:
                    complete.pop(outline, None)
                else:
                    complete[outline] = text
            related["all"] = complete

    entries = (saved_edits if mode == "edits" else effective_document)(
        console, selected, on_read, progress, on_snapshot=capture)
    after = json.loads(console.command("user_dictionary_status"))
    if any(status.get(field) != after.get(field) for field in ("generation", "tail")):
        raise ValueError("The dictionary changed while loading. Reopen the editor.")
    progress("Preparing JSON for the editor…")
    document = make_document(console, selected, entries, mode)
    if key is not None:
        related[mode] = entries
        if force:
            for old in list(_documents):
                if old[:-1] == key[:-1]:
                    del _documents[old]
        for related_mode, data in related.items():
            related_key = key[:-1] + (related_mode,)
            if related_key not in _documents and len(_documents) >= 6:
                _documents.pop(next(iter(_documents)))
            _documents[related_key] = dict(document if related_mode == mode else make_document(console, selected, data, related_mode))
    return document


def change_document(console, selected, request, progress, *, apply=False):
    mode = editor_mode(request.get("mode", "all"))
    parse = parse_edits if mode == "edits" else parse_source
    desired, names = parse(request.get("source"))
    current = read_document(console, selected, progress, mode=mode)
    if request.get("version") != current["version"]:
        raise ValueError("This dictionary changed since you opened it. Copy your draft before reloading from the keyboard.")
    original, _ = parse(current["source"])
    added = desired.keys() - original.keys()
    updated = {key for key in desired.keys() & original.keys() if desired[key] != original[key]}
    removed = original.keys() - desired.keys()
    version = fingerprint(selected, console.serial, desired, mode)
    token = hashlib.sha256((current["version"] + version).encode()).hexdigest()
    plan = dict(added=len(added), updated=len(updated), removed=len(removed), count=len(desired), mode=mode, token=token)
    if not apply:
        return plan
    if request.get("token") != token:
        raise ValueError("Preview these JSON changes before saving them.")
    status = json.loads(console.command("user_dictionary_status"))
    if not status.get("ready") or status.get("read_only"):
        raise ValueError("The dictionary store is unavailable or read-only.")
    if mode == "edits" and removed and selected["kind"] == "snapshot":
        if console.command("native_install update snapshot-reset-capabilities") != "OK RESET1":
            raise ValueError("Update the left firmware to undo saved edits on an added compact dictionary.")
    total, saved = len(added) + len(updated) + len(removed), 0
    try:
        for strokes in sorted(added | updated):
            if desired[strokes] is None:
                checked(console, f"native_remove_entry {selected['id']} {names[strokes]}")
            else:
                import_entries(console, selected["id"], {strokes: desired[strokes]}, names, lambda _: None)
            saved += 1
            progress(f"Saving dictionary changes: {saved}/{total}")
        _, original_names = parse(current["source"])
        for strokes in sorted(removed):
            verb = "reset" if mode == "edits" else "remove"
            checked(console, f"native_{verb}_entry {selected['id']} {original_names[strokes]}")
            saved += 1
            progress(f"Saving dictionary changes: {saved}/{total}")
    except (RuntimeError, OSError, TimeoutError, ValueError) as error:
        try:
            checked(console, "native_entry abort")
        except (RuntimeError, OSError, TimeoutError):
            pass
        raise RuntimeError(f"Saved {saved}/{total} changes before stopping: {error}. "
                           "Copy your draft before reloading from the keyboard and retrying.") from error
    data = {names[strokes]: None if text is None else text.decode("utf-8") for strokes, text in desired.items()}
    return {**current, "source": json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            "version": version, "count": len(desired), "changes": total}
