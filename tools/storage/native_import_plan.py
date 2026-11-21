"""Preview and publish a new named compact dictionary."""
import hashlib
import json
import zlib

from build_native_image import SEGMENT
from native_snapshot import compile_snapshot, plan_named_snapshot, stage_allocator
from native_update import NoSpace, Snapshot, abort, publish, read_snapshot, update_command, validate_name


def metadata(payload):
    return Snapshot(lambda address, size: payload[:size], 0, len(payload), zlib.crc32(payload),
                    metadata_only=True)


def owned(view):
    return {0, 1, 255} | {segment for start, count, *_ in view.extents for segment in range(start, start + count)}


def consolidation_cost(after, image_segments, index_records):
    # A future same-size consolidation needs an image, up to two replacement
    # indexes and staging metadata. Probe the same allocator without rebuilding
    # those files or relocating the image a second time.
    if sum(extent[4] < 3 for extent in after.extents) + sum(bool(count) for count in index_records) > 253:
        raise NoSpace("Catalog extent limit")
    allocate = stage_allocator(owned(after))
    allocate(image_segments)
    total = image_segments
    for count in index_records:
        if count:
            blocks = (count * 4 + SEGMENT - 1) // SEGMENT
            allocate(blocks)
            total += blocks
    allocate(1)
    return total + 1


def plan_compact_import(snapshot, protected, name, compiled, limit):
    report = {"bytes": len(compiled.data), "image_segments": (len(compiled.data) + SEGMENT - 1) // SEGMENT}
    try:
        update = plan_named_snapshot(snapshot, protected, name, compiled, limit=limit)
    except NoSpace as error:
        return {**report, "reason": str(error)}, None
    return {**report, "staging_segments": len(update.segments)}, update


def named_import(console, name, entries, *, stage=False, expected=None, progress=print):
    validate_name(name)
    if not entries:
        raise ValueError("This dictionary has zero entries.")
    if update_command(console, "snapshot-capabilities") != "OK SNAPSHOT2":
        raise RuntimeError("This keyboard does not offer compact dictionary imports.")
    capability = update_command(console, "named-capabilities").split()
    if len(capability) != 3 or capability[:2] != ["OK", "NAMED1"]:
        raise RuntimeError("This keyboard does not offer named dictionary imports.")
    limit = int(capability[2])
    progress("Compiling the compact dictionary…")
    compiled = compile_snapshot(name, entries)

    answer = update_command(console, "begin snapshot-named")
    published = False
    try:
        progress("Reading the catalog and checking available space…")
        snapshot, protected = read_snapshot(console, answer, metadata_only=True)
        status = json.loads(console.command("user_dictionary_status"))
        generation, tail = status.get("generation"), status.get("tail")
        if type(generation) is not int or type(tail) is not int or not 0 <= tail <= 256:
            raise RuntimeError("The keyboard did not report its catalog generation and pending edits.")
        report, update = plan_compact_import(snapshot, protected, name, compiled, limit)
        report["entries"] = len(entries)
        if update is not None:
            after = metadata(update.payload)
            # Each uncheckpointed edit can add at most one record to each
            # index. If this bound cannot fit, check the exact live counts.
            sizes = {kind: size // 4 for _, _, size, _, kind, _ in snapshot.extents if kind in (3, 4)}
            try:
                consolidation_cost(after, report["image_segments"], tuple(sizes.get(kind, 0) + tail for kind in (3, 4)))
            except NoSpace:
                progress("Checking live journal usage for future consolidation…")
                live, _ = read_snapshot(console, answer)
                counts = len(live.entries), sum(entry.operation == 0 for entry in live.entries.values())
                try:
                    consolidation_cost(after, report["image_segments"], counts)
                except NoSpace as error:
                    report["reason"] = f"A same-size consolidation could not stage. {error}"
                    update = None
        token = None
        if update is not None:
            digest = hashlib.sha256(compiled.data)
            digest.update(snapshot.payload)
            digest.update(json.dumps([console.serial, generation, tail, sorted(protected)],
                                     separators=(",", ":")).encode())
            token = digest.hexdigest()
        if stage:
            if expected is not None and expected != token:
                raise RuntimeError("The import plan changed. Preview it again before staging.")
            if update is None:
                raise NoSpace(report["reason"])
            publish(console, update, progress)
            published = True
        return {**report, "token": token}
    finally:
        if not published:
            abort(console)
