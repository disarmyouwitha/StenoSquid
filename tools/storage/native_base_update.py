"""Replace pinned compiled bases by staging new extents beside the old catalog."""
from dataclasses import dataclass
import json
import struct
import zlib

from build_native_image import SEGMENT, SOURCES, XIP
from compile_dict import DictionaryCollectionBuilder
from hybrid import format_stroke
from jsc4 import reader
from manifest import BASES, ROOT
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
