"""Decode the experimental flash map, based only on catalog ownership."""
import struct

from build_native_image import LARGE_POOL_END, SEGMENT, SMALL_POOL_END, XIP
from native_snapshot import inspect_free
from native_update import read_snapshot, row_name, update_command

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
    if (len(parts) != 7 or parts[:2] != ["OK", "MAP1"]
            or any(len(row) != SMALL_POOL_END + 1 for row in parts[5:])):
        raise ValueError("Invalid flash map response")
    return parse_maps(*parts[2:])


def parse_maps(selected, access, states, first, second):
    if selected not in ("0", "1") or access not in ("w", "r", "p") or len(states) != 2 or set(states) - set("ve?"):
        raise ValueError("Invalid flash map status")
    maps = first, second
    pool_end = len(first) - 1
    if pool_end not in (SMALL_POOL_END, LARGE_POOL_END):
        raise ValueError("Invalid flash map size")
    for state, data in zip(states, maps):
        if len(data) != pool_end + 1 or data[:2] != "cc" or data[-1] != "x" or set(data) - REGIONS.keys():
            raise ValueError("Invalid flash map geometry")
        if (state == "e" and set(data[2:pool_end]) != {"."}) or (state == "?" and set(data[2:pool_end]) != {"?"}):
            raise ValueError("Invalid catalog region")
        if state == "v" and "?" in data:
            raise ValueError("Incomplete catalog region")
    active_slot = int(selected)
    active, alternate = maps[active_slot], maps[1 - active_slot]
    if states[active_slot] == "e":
        raise ValueError("Active catalog is missing")
    whole = "?" not in states and access != "r"
    protected = {i for i, (primary, old) in enumerate(zip(active, alternate)) if primary != "." or old != "."}
    free, longest = inspect_free(protected, pool_end) if whole else (None, None)
    segments = []
    for idx, (primary, old) in enumerate(zip(active, alternate)):
        if idx in (0, 1, pool_end):
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
                             shared=primary not in ".?" and old not in ".?" and 2 <= idx < pool_end))
    return dict(segments=segments, active_slot=active_slot, states=list(states), free_segments=free,
                largest_free_run=longest, reserve=3, staging_blocked=access != "w")


def read_layout(console):
    reply = update_command(console, "layout-map")
    if not reply.startswith("OK MAP2 "):
        return parse_layout(reply)
    maps = ["", ""]
    expected_header = None
    for first in range(0, LARGE_POOL_END + 1, 256):
        if first:
            reply = update_command(console, f"layout-map {first}")
        page = reply.split()
        if (len(page) != 11 or page[:4] != ["OK", "MAP2", str(LARGE_POOL_END + 1), str(first)]
                or any(len(data) != 256 for data in page[9:])):
            raise ValueError("Invalid flash map page")
        if expected_header is None:
            expected_header = page[4:9]
        elif page[4:9] != expected_header:
            raise RuntimeError("The catalog changed during the flash map scan. Scan again.")
        for slot in range(2):
            maps[slot] += page[9 + slot]
    return parse_maps(*expected_header[2:], *maps)


def validate_compact_layout(read, start, size):
    """Check a single dictionary’s compact tables and hints."""
    def at(offset, length):
        if offset < 0 or length < 0 or offset + length > size:
            raise ValueError("Compact image metadata is outside its extent")
        return read(start + offset, length)

    base = XIP + start
    magic, count = struct.unpack("<4sI", at(0, 8))
    if magic != b"JSC4" or count != 1:
        raise ValueError("Unsupported compact image metadata")
    pointer, = struct.unpack("<I", at(36, 4))
    magic, directory, members = struct.unpack("<4sII", at(size - 12, 12))
    if magic != b"SHI1" or members != 1 or directory + 20 != size:
        raise ValueError("Unsupported compact hint directory")
    hint_start, hint_count = struct.unpack("<II", at(directory, 8))
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
    if total > size:
        raise ValueError("Compact tables exceed the image size")


def read_images(console):
    """Use the catalog and bounded image metadata reads, without walking entries."""
    answer = update_command(console, "begin metadata")
    try:
        snapshot, _ = read_snapshot(console, answer, metadata_only=True)
        images = []
        for row in snapshot.rows:
            identity, _, extent, member = struct.unpack_from("<IIHB", row)
            if extent == 65535:
                continue
            if member:
                raise ValueError("Expected individual dictionary images")
            start, segments, size, _, _, _ = snapshot.extents[extent]
            images.append(dict(id=identity, name=row_name(row), extent=extent, start=start, segments=segments,
                               bytes=size, kind="s" if row[11] else "b"))
        return sorted(images, key=lambda image: image["start"])
    finally:
        update_command(console, "abort", timeout=5)
