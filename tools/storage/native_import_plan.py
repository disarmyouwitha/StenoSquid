"""Measured, opt-in preflight for a new independent dictionary."""
import hashlib
import json
import struct
import zlib

from build_native_image import SEGMENT
from native_snapshot import compile_snapshot, inspect_free, plan_change, plan_named_snapshot
from native_update import Edit, NoSpace, Snapshot, abort, plan, publish, read_snapshot, update_command, validate_name


def metadata(payload):
    return Snapshot(lambda address, size: payload[:size], 0, len(payload), zlib.crc32(payload),
                    metadata_only=True)


def owned(view):
    return {0, 1, 255} | {segment for start, count, *_ in view.extents for segment in range(start, start + count)}


def cost(update, protected, current, logical_bytes):
    after = owned(metadata(update.payload))
    return dict(logical_bytes=logical_bytes, steady_segments=len(after) - 1,
                added_segments=len(after) - len(current), staging_segments=len(update.segments),
                peak_segments=len(set(protected) | set(update.segments)) - 1,
                free_after_commit=255 - (len(after) - 1), eligible=True)


def compare(snapshot, protected, name, entries, limit, compiled, compile_problem=None):
    current = owned(snapshot)
    report = {"options": {}, "recommended": None}
    options = report["options"]
    updates = {}
    journal_bytes = sum(Edit(0, snapshot.next_id, 0, strokes, text).span + 8 for strokes, text in entries.items())
    free, largest = inspect_free(protected)
    source = hashlib.sha256(name.encode("ascii"))
    for strokes, text in sorted(entries.items()):
        source.update(struct.pack(f"<BH{len(strokes)}I", len(strokes), len(text), *strokes))
        source.update(text)
    report.update(free_segments=free, largest_free_run=largest, reserve=3, entries=len(entries),
                  source_sha256=source.hexdigest())
    for key, logical, planner in (
        ("journal", journal_bytes, lambda: plan(snapshot, protected, entries, name=name, limit=limit)),
        ("snapshot", len(compiled.data) if compiled else None,
         lambda: plan_named_snapshot(snapshot, protected, name, compiled, limit=limit)),
    ):
        if key == "snapshot" and compiled is None:
            options[key] = dict(eligible=False, reason=compile_problem, logical_bytes=None, eligibility="cannot-stage")
            continue
        try:
            update = planner()
        except NoSpace as error:
            options[key] = dict(eligible=False, reason=str(error), logical_bytes=logical, eligibility="cannot-stage")
            continue
        updates[key] = update
        options[key] = cost(update, protected, current, logical)
        if key == "snapshot":
            after = metadata(update.payload)
            options[key]["contiguous_segments"] = (logical + SEGMENT - 1) // SEGMENT
            # A new named snapshot adds no journal records. Model consolidation
            # of its initial contents with the existing shared edit indexes.
            after.entries = snapshot.entries
            after.highwater = snapshot.highwater
            try:
                second = plan_change(after, owned(after), snapshot.next_id, compiled, consolidate=True)
            except NoSpace as error:
                options[key].update(eligible=False, eligibility="usb-rebuild-unqualified",
                                    reason=f"The snapshot fits today, but same-size consolidation cannot stage. {error} "
                                           "USB rebuilding is unavailable in Manager.")
            else:
                options[key]["eligibility"] = "copy-on-write"
                options[key]["consolidation_staging_segments"] = len(second.segments)
    journal, compact = options["journal"], options["snapshot"]
    if compact["eligible"] and not journal["eligible"]:
        report.update(recommended="snapshot", rationale="Only the compact plan meets the staging and reserve checks.")
    elif journal["eligible"]:
        report.update(recommended="journal", rationale="The journal fits and the compact savings are below the selection threshold.")
        if compact["eligible"]:
            byte_saving = journal_bytes - compact["logical_bytes"]
            segment_saving = journal["steady_segments"] - compact["steady_segments"]
            if segment_saving > 0 and (byte_saving >= 1024 * 1024 or
                                      (byte_saving >= SEGMENT and byte_saving * 4 >= journal_bytes)):
                report.update(recommended="snapshot", rationale="The compact image saves material space and a same-size consolidation fits.")
    else:
        report["rationale"] = ("Neither format has a qualified copy-on-write plan. Nothing can be staged. "
                               "USB rebuilding is unavailable in Manager.")
    return report, updates


def fingerprint(snapshot, protected, report):
    values = json.dumps(report, sort_keys=True).encode("utf-8")
    data = snapshot.payload + struct.pack("<Q", snapshot.highwater) + bytes(sorted(protected)) + values
    return hashlib.sha256(data).hexdigest()


def named_import(console, name, entries, *, preference="auto", stage=False, expected=None, progress=print):
    validate_name(name)
    if not entries:
        raise ValueError("This dictionary has zero entries.")
    if preference not in ("auto", "journal", "snapshot"):
        raise ValueError("Choose Automatic, Journal, or Compact snapshot.")
    version = update_command(console, "snapshot-capabilities")
    if version != "OK SNAPSHOT2":
        raise RuntimeError("This preflight requires the experimental snapshot lifecycle firmware.")
    capability = update_command(console, "named-capabilities").split()
    if len(capability) != 3 or capability[:2] != ["OK", "NAMED1"]:
        raise RuntimeError("This firmware does not support named dictionary imports.")
    limit = int(capability[2])
    compiled = compile_problem = None
    try:
        compiled = compile_snapshot(name, entries)
    except ValueError as error:
        compile_problem = str(error)

    answer = update_command(console, "begin snapshot-named")
    active = True
    published = False
    try:
        snapshot, protected = read_snapshot(console, answer)
        report, updates = compare(snapshot, protected, name, entries, limit, compiled, compile_problem)
        selected = report["recommended"] if preference == "auto" else preference
        report["selected"] = selected if selected and report["options"][selected]["eligible"] else None
        token = fingerprint(snapshot, protected, report)
        report["token"] = token
        if not stage:
            return report
        if expected is not None and expected != token:
            raise RuntimeError("The import plan changed. Preview it again before staging.")
        if report["selected"] is None:
            option = report["options"].get(selected, {})
            raise NoSpace(option.get("reason", report["rationale"]))
        if selected == "journal":
            # The same protected view must still be current under the actual
            # journal staging operation, before the host writes any byte.
            update_command(console, "abort")
            active = False
            answer = update_command(console, "begin named")
            active = True
            check, check_protected = read_snapshot(console, answer)
            # Only differences in protected allocation, catalog or append
            # highwater can invalidate a plan prepared in the first session.
            expected_report = {key: value for key, value in report.items() if key != "token"}
            if fingerprint(check, check_protected, expected_report) != token:
                raise RuntimeError("The import plan changed. Preview it again before staging.")
        progress(f"Staging the {selected} plan for {name}.")
        publish(console, updates[selected], progress)
        published = True
        return report
    finally:
        if active and not published:
            abort(console)
