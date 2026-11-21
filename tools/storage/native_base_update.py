"""Replace pinned compiled bases by staging new extents beside the old catalog."""
from dataclasses import dataclass
import hashlib
import json
import struct
import zlib

from build_native_image import SEGMENT, SOURCES, XIP
from compile_dict import DictionaryCollectionBuilder
from hybrid import format_stroke
from jsc4 import reader
from manifest import BASES, ROOT
from native_snapshot import CompactImage, inspect_free, stage_allocation
from native_update import EXTENT, NoSpace, Update, abort, publish, read_flash, read_snapshot, row_name, update_command
from prepare import fetch
from verify_mapped_dict import verify

GROUPS = (tuple(row[1] for row in SOURCES[1:6]), tuple(row[1] for row in SOURCES[6:]))
PINS = {
    ("Lapwing Base" if label == "lapwing-base" else label): (url, file, digest, base["directory"])
    for base in BASES.values()
    for url, file, label, digest in base["sources"]
}


def changed_pins(catalog):
    return [
        row["name"] for row in catalog
        if row["kind"] == "base" and row["name"] in PINS
        and row["source_sha256"] != PINS[row["name"]][2]
    ]


@dataclass
class Image:
    definitions: list
    contents: bytes
    pointers: list
    digests: dict

    def relocate(self, address):
        blob = bytearray(self.contents)
        for pointer in self.pointers:
            offset = struct.unpack_from("<I", blob, pointer)[0]
            if not 0 <= offset < len(blob):
                raise ValueError("Compiler emitted an out-of-bounds dictionary pointer")
            struct.pack_into("<I", blob, pointer, address + offset)
        return blob


def prepare(group):
    definitions = []
    digests = {}
    for name in group:
        url, file, digest, directory = PINS[name]
        path = ROOT / "build" / directory / file
        path.parent.mkdir(parents=True, exist_ok=True)
        fetch(url, path, digest)
        definitions.append((name, json.loads(path.read_text(encoding="utf-8"))))
        digests[name] = bytes.fromhex(digest)
    builder = DictionaryCollectionBuilder(0)
    blob = builder.build(definitions)
    for member, (_, entries) in enumerate(definitions):
        verify(entries, blob, 0, member, len(group))
    return Image(definitions, blob, builder.pointer_offsets, digests)


def plan_replacements(snapshot, protected, compiled, read_original=None):
    """Keep everything except the selected compiled extents and source digests."""
    rows = [bytearray(row) for row in snapshot.rows]
    extents = list(snapshot.extents)
    groups = {}
    for row in rows:
        base, member = struct.unpack_from("<HB", row, 8)
        if base != 65535:
            groups.setdefault(base, []).append((member, row))
    free = set(range(2, 255)) - set(protected)
    blocks, changed, conflicts = {}, [], []

    def allocate(count):
        if len(free) >= count + 3:
            for segment in sorted(free):
                run = range(segment, segment + count)
                if all(at in free for at in run):
                    free.difference_update(run)
                    return segment
        raise NoSpace("The replacement images cannot fit beside the current store. No dictionaries changed.")

    overlays_by_id = {}
    for entry in getattr(snapshot, "entries", {}).values():
        overlays_by_id.setdefault(entry.identity, []).append(entry)

    planned = set()
    for base, members in groups.items():
        members.sort()
        group = tuple(row_name(row) for _, row in members)
        image = compiled.get(group)
        if not image:
            continue
        if [member for member, _ in members] != list(range(len(group))):
            raise ValueError("Installed packed image members do not match the pinned manifest.")
        planned.add(group)
        replaced = [(member, row) for member, row in members if row[64:96] != image.digests[row_name(row)]]
        if not replaced:
            continue
        count = (len(image.contents) + SEGMENT - 1) // SEGMENT
        segment = allocate(count)
        blob = image.relocate(XIP + segment * SEGMENT)
        if read_original:
            old_start, _, old_size, crc, _, _ = extents[base]
            original = None
            for member, row in replaced:
                edits = overlays_by_id.get(struct.unpack_from("<I", row)[0], [])
                if not edits:
                    continue
                old_address = old_start * SEGMENT
                if original is None:
                    original = read_original(old_address, old_size)
                    if zlib.crc32(original) != crc:
                        raise ValueError("Installed base checksum failed. No dictionaries changed.")
                old = reader(original, XIP + old_address, member, len(group))
                new = reader(blob, XIP + segment * SEGMENT, member, len(group))
                for entry in edits:
                    outline = "/".join(map(format_stroke, entry.strokes))
                    before, after = old(outline), new(outline)
                    if before != after:
                        conflicts.append(dict(dictionary=row_name(row), outline=outline, before=before, after=after,
                                              local=entry.text.decode() if not entry.operation else None))
        for member, row in replaced:
            name = row_name(row)
            row[64:96] = image.digests[name]
            changed.append(name)
        extents[base] = (segment, count, len(blob), zlib.crc32(blob), 1, 0)
        for i in range(count):
            blocks[segment + i] = blob[i * SEGMENT:(i + 1) * SEGMENT]
    if planned != compiled.keys():
        raise ValueError("Installed base groups differ from this version’s pinned manifest.")
    if not changed:
        return None, changed, conflicts
    metadata = allocate(1)
    payload = snapshot.payload[:32] + b"".join(rows) + b"".join(EXTENT.pack(*row) for row in extents)
    blocks[metadata] = payload
    return Update(blocks, metadata, payload, 0, 0), changed, conflicts


def replace_pinned(console, catalog, progress=print):
    changes = changed_pins(catalog)
    if not changes:
        return [], []
    if console.command("native_install update base-capabilities") != "OK BASE1":
        raise RuntimeError("Flash the matching Manager firmware before replacing bases.")
    compiled = {}
    for group in GROUPS:
        if any(name in changes for name in group):
            progress(f"Building and checking pinned image for {group[-1]}…")
            compiled[group] = prepare(group)
    answer = update_command(console, "begin base")
    try:
        progress("Checking free space, journal integrity and upstream conflicts…")
        snapshot, protected = read_snapshot(console, answer)
        update, changed, conflicts = plan_replacements(
            snapshot, protected, compiled, lambda start, size: read_flash(console, start, size)
        )
        if not update:
            update_command(console, "abort")
            return changed, conflicts
        publish(console, update, progress)
        return changed, conflicts
    except BaseException:
        abort(console)
        raise


PACKAGES = {"lapwing": (6, GROUPS[0]), "plover": (7, GROUPS[1])}
FACTORY_ABSENT = 2


def plan_package(snapshot, protected, package, restore=False, compiled=None):
    package_id, names = PACKAGES[package]
    rows = [bytearray(row) for row in snapshot.rows]
    by_name = {row_name(row): row for row in rows}
    if any(name not in by_name for name in names):
        raise ValueError("Pinned package rows are missing")
    members = [by_name[name] for name in names]
    if [struct.unpack_from("<I", row)[0] for row in members] != list(range(package_id - len(names) + 1, package_id + 1)):
        raise ValueError("Pinned package IDs do not match")
    extents = list(snapshot.extents)
    blocks, added, write = stage_allocation(protected)
    if restore:
        if any(row[11] != FACTORY_ABSENT for row in members):
            raise ValueError("The factory package is already installed")
        if not compiled:
            raise ValueError("Verified pinned sources are required to restore a package")
        if len(extents) == 253:
            raise NoSpace("Catalog extent limit")
        base = len(extents)
        write(CompactImage(compiled.contents, compiled.pointers), 1)
        extents.extend(EXTENT.unpack(row) for row in added)
        for member, row in enumerate(members):
            struct.pack_into("<HBB", row, 8, base, member, 0)
            row[64:96] = compiled.digests[row_name(row)]
        package_segments = extents[base][1]
    else:
        if any(row[11] == FACTORY_ABSENT for row in members):
            raise ValueError("The factory package is already uninstalled")
        base = struct.unpack_from("<H", members[0], 8)[0]
        if base >= len(extents) or any(
                struct.unpack_from("<HB", row, 8) != (base, member) or row[11]
                for member, row in enumerate(members)):
            raise ValueError("The factory package does not have one shared extent")
        for row in rows:
            reference = struct.unpack_from("<H", row, 8)[0]
            if row in members:
                struct.pack_into("<IHBB", row, 4, 0, 65535, 0, FACTORY_ABSENT)
            elif reference == base:
                raise ValueError("The package extent is shared with another dictionary")
            elif base < reference < 65535:
                struct.pack_into("<H", row, 8, reference - 1)
        _, package_segments, *_ = extents.pop(base)
    prefix = bytearray(snapshot.payload[:32])
    struct.pack_into("<H", prefix, 6, len(extents))
    payload = bytes(prefix) + b"".join(rows) + b"".join(EXTENT.pack(*row) for row in extents)
    metadata = write(payload, 0)
    current = {0, 1, 255}
    for start, count, *_ in snapshot.extents:
        current.update(range(start, start + count))
    target = {0, 1, 255}
    for start, count, *_ in extents:
        target.update(range(start, start + count))
    confirmed = hashlib.sha256(snapshot.payload)
    confirmed.update(bytes(sorted(protected)))
    confirmed.update(payload)
    for segment in sorted(blocks):
        confirmed.update(blocks[segment])
    free, run = inspect_free(protected)
    after, after_run = inspect_free(target)
    report = dict(package=package, restoring=restore, package_segments=package_segments,
                  staging_segments=len(blocks), free_now=free, largest_run_now=run,
                  free_after_one_root=inspect_free(current | target)[0],
                  free_after_both_roots=after, largest_run_after_both_roots=after_run,
                  package_segments_released=0 if restore else package_segments,
                  retained_only_segments=len(set(protected) - current),
                  confirmation=confirmed.hexdigest())
    return Update(blocks, metadata, payload, 0, 0), report


def factory_package(console, package, *, restore=False, confirmation=None, progress=print):
    if package not in PACKAGES:
        raise ValueError("Select Lapwing or Plover")
    if console.command("native_install update factory-capabilities") != "OK FACTORY1":
        raise RuntimeError("Flash the matching left firmware before managing factory packages.")
    identity, names = PACKAGES[package]
    compiled = prepare(names) if restore else None
    answer = update_command(console, f"begin factory {identity}")
    published = False
    try:
        snapshot, protected = read_snapshot(console, answer, metadata_only=True)
        update, report = plan_package(snapshot, protected, package, restore, compiled)
        approval = hashlib.sha256(f"{console.serial}:{report['confirmation']}".encode()).hexdigest()
        report["confirmation"] = approval
        if confirmation is not None:
            if confirmation != approval:
                raise ValueError("The approved plan changed. Preview the package again.")
            publish(console, update, progress)
            published = True
        return report
    finally:
        if not published:
            abort(console)
