"""Decode the experimental flash map, based only on catalog ownership."""
import struct

from build_native_image import SEGMENT, XIP
from native_snapshot import inspect_free
from native_update import read_flash, read_snapshot, row_name, update_command

REGIONS = {
    "c": "Catalog roots",
    "x": "Reserved final segment",
    "b": "Pinned compact image",
    "s": "User snapshot",
    "h": "Latest journal",
    "j": "Journal",
    "f": "Forward index",
    "r": "Reverse index",
    ".": "Unreferenced",
    "?": "Unknown",
}


def parse_layout(response):
    parts = response.split()
    if len(parts) != 7 or parts[:2] != ["OK", "MAP1"]:
        raise ValueError("Invalid flash map response")
    _, _, selected, access, states, first, second = parts
    if selected not in ("0", "1") or access not in ("w", "r", "p") or len(states) != 2 or set(states) - set("ve?"):
        raise ValueError("Invalid flash map status")
    maps = first, second
    for state, data in zip(states, maps):
        if len(data) != 256 or data[:2] != "cc" or data[-1] != "x" or set(data) - REGIONS.keys():
            raise ValueError("Invalid flash map geometry")
        if (state == "e" and set(data[2:255]) != {"."}) or (state == "?" and set(data[2:255]) != {"?"}):
            raise ValueError("Invalid catalog region")
        if state == "v" and "?" in data:
            raise ValueError("Incomplete catalog region")
    active_slot = int(selected)
    active, alternate = maps[active_slot], maps[1 - active_slot]
    if states[active_slot] == "e":
        raise ValueError("Active catalog is missing")
    whole = "?" not in states and access != "r"
    protected = {i for i, (primary, old) in enumerate(zip(active, alternate)) if primary != "." or old != "."}
    free, longest = inspect_free(protected) if whole else (None, None)
    segments = []
    for idx, (primary, old) in enumerate(zip(active, alternate)):
        if idx in (0, 1, 255):
            kind = primary
        elif primary != ".":
            kind = primary
        elif old in "jh":
            kind = "retained-journal"
        elif old not in ".?":
            kind = "retained"
        else:
            kind = "." if whole else "?"
        segments.append(dict(index=idx, kind=kind, active=REGIONS[primary], alternate=REGIONS[old],
                             shared=primary not in ".?" and old not in ".?" and 2 <= idx < 255))
    return dict(segments=segments, active_slot=active_slot, states=list(states), free_segments=free,
                largest_free_run=longest, reserve=3, staging_blocked=access != "w")


def read_layout(console):
    return parse_layout(update_command(console, "layout-map"))


def compact_member_sizes(read, start, size):
    """Count member-specific bytes without reading the shared translation pool."""
    def at(offset, length):
        if offset < 0 or length < 0 or offset + length > size:
            raise ValueError("Compact image metadata is outside its extent")
        return read(start + offset, length)

    base = XIP + start
    magic, count = struct.unpack("<4sI", at(0, 8))
    if magic != b"JSC4" or not 1 <= count <= 64:
        raise ValueError("Unsupported compact image metadata")
    definitions = struct.unpack(f"<{count}I", at(36, 4 * count))
    magic, directory, members = struct.unpack("<4sII", at(size - 12, 12))
    if magic != b"SHI1" or members != count or directory + 8 * count + 12 != size:
        raise ValueError("Unsupported compact hint directory")
    hints = list(struct.iter_unpack("<II", at(directory, 8 * count)))

    sizes = []
    for pointer, (hint_start, hint_count) in zip(definitions, hints):
        definition = at(pointer - base, 16)
        maximum = definition[1]
        name, _, strokes = struct.unpack_from("<III", definition, 4)
        if not 1 <= maximum <= 16 or definition[2] != 0 or hint_start + 8 * hint_count > directory:
            raise ValueError("Unsupported compact member layout")
        name_offset = name - base
        name_length = at(name_offset, min(45, size - name_offset)).index(b"\0") + 1
        # Collection pointer, definition, name, stroke table, and hint records/directory entry.
        total = 4 + 16 + name_length + 12 * maximum + 8 * (hint_count + 1)
        groups = struct.iter_unpack("<III", at(strokes - base, 12 * maximum))
        for length, (mask, data, offsets) in enumerate(groups, 1):
            if not mask:
                continue
            if not 127 <= mask <= 0x7fffff or mask & (mask + 1):
                raise ValueError("Invalid compact hash mask")
            hash_bytes = ((mask + 1) // 128) * 20
            end = offsets - base + hash_bytes
            *masks, rank = struct.unpack("<5I", at(end - 20, 20))
            entries = ((rank + 1) & 0xffffffff) + sum(bits.bit_count() for bits in masks)
            data_bytes = entries * (3 + 3 * length)
            if not 0 < entries <= mask + 1 or not 0 <= data - base <= offsets - base - data_bytes:
                raise ValueError("Invalid compact data span")
            total += data_bytes + hash_bytes
        sizes.append(total)
    if sum(sizes) > size:
        raise ValueError("Compact member totals exceed the image size")
    return sizes


def read_images(console):
    """Use the catalog and bounded image metadata reads, without walking entries."""
    try:
        answer = update_command(console, "begin metadata")
    except RuntimeError as error:
        if str(error) != "ERR No native update active":
            raise
        answer = update_command(console, "begin backup")
    try:
        snapshot, _ = read_snapshot(console, answer, metadata_only=True)
        images = {}
        for row in snapshot.rows:
            identity, _, extent, member = struct.unpack_from("<IIHB", row)
            if extent == 65535:
                continue
            start, segments, size, _, _, _ = snapshot.extents[extent]
            image = images.setdefault(extent, dict(start=start, segments=segments, bytes=size,
                                                   kind="s" if row[11] else "b", dictionaries=[]))
            image["dictionaries"].append(dict(id=identity, name=row_name(row), member=member))
        for image in images.values():
            dictionaries = image["dictionaries"]
            if len(dictionaries) == 1:
                sizes = [image["bytes"]]
            else:
                sizes = compact_member_sizes(lambda offset, size: read_flash(console, offset, size),
                                             image["start"] * SEGMENT, image["bytes"])
            for dictionary in dictionaries:
                member = dictionary.pop("member")
                dictionary["bytes"] = sizes[member] if len(dictionaries) > 1 else sizes[0]
            image["shared_bytes"] = image["bytes"] - sum(item["bytes"] for item in dictionaries)
        return sorted(images.values(), key=lambda image: image["start"])
    finally:
        update_command(console, "abort", timeout=5)
