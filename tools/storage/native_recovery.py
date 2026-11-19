"""Self-contained experimental backups and offline reconstruction.

The bundle holds the selected catalog and its exact referenced extents. Keeping
their physical addresses preserves mapped pointers and both journal indexes.
Offline reconstruction never exposes the result to the USB provisioner.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import zlib

from build_native_image import LIMIT, SEGMENT, SOURCES, XIP, catalog_header
from compact_search_index import validate_search_index
from hybrid import compact_entries
from native_update import EXTENT, Snapshot, row_name, validate_name

FORMAT = "stenosquid-backup-2"
LAYOUTS = ("Mac U.S.", "Windows U.S. (plain text)")


def digest(backup):
    manifest = {key: value for key, value in backup.items() if key != "sha256"}
    data = json.dumps(manifest, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(data.encode("ascii")).hexdigest()


def pack(data):
    return base64.b64encode(data).decode("ascii")


def unpack(encoded, expected=None):
    if not isinstance(encoded, str) or len(encoded) > (LIMIT + 2) // 3 * 4:
        raise ValueError("Invalid recovery data size")
    data = base64.b64decode(encoded, validate=True)
    if expected is not None and len(data) != expected:
        raise ValueError("Recovery data size differs from its catalog")
    return data


def descriptors(payload):
    if not 32 <= len(payload) <= 32 + 64 * 96 + 253 * 24:
        raise ValueError("Invalid recovery catalog size")
    next_id, rows, count = struct.unpack_from("<IHH", payload)
    end = 32 + rows * 96
    if next_id < 8 or rows > 64 or count > 253 or any(payload[16:32]) or len(payload) != end + count * 24:
        raise ValueError("Invalid recovery catalog shape")
    used, kinds = {0, 1, 255}, set()
    result = list(EXTENT.iter_unpack(payload[end:]))
    for start, span, size, crc, kind, epoch in result:
        run = set(range(start, start + span))
        if (not 2 <= start < 255 or not 1 <= span <= 255 - start
                or not 0 < size <= span * SEGMENT or run & used or kind not in (1, 2, 3, 4)
                or (kind == 2 and (span != 1 or size != SEGMENT or not epoch or crc))
                or (kind != 2 and epoch) or (kind in (3, 4) and (size % 4 or kind in kinds))):
            raise ValueError("Invalid or overlapping recovery extent")
        used.update(run)
        kinds.add(kind)
    if 2 not in kinds:
        raise ValueError("Recovery catalog is missing its journal")
    return result


def capture(console_read, snapshot, layout):
    payload = snapshot.payload
    backup = dict(format=FORMAT, host_layout=layout, catalog=pack(payload), extents=[
        pack(console_read(start * SEGMENT, size)) for start, _, size, _, _, _ in descriptors(payload)
    ])
    backup["sha256"] = digest(backup)
    reconstruct(backup)
    return backup


def validate_rows(snapshot, image):
    factory = {identity: name for identity, name, _, source in SOURCES if source}
    references, names, ids = {}, set(), set()
    counts = {}
    for row in snapshot.rows:
        identity, enabled, base, member, marker, name_bytes = struct.unpack_from("<IIHBBB", row)
        name = row_name(row)
        if (not 0 < identity < snapshot.next_id or identity in ids or name in names
                or enabled > 1 or not 1 <= name_bytes <= 44 or any(row[13:16])
                or any(row[16 + name_bytes:64]) or marker > 1):
            raise ValueError("Invalid recovery dictionary row")
        names.add(name)
        ids.add(identity)
        if identity in factory:
            if name != factory[identity] or marker or base == 65535 or not any(row[64:96]):
                raise ValueError("Invalid pinned source identity")
        else:
            if name != "user_dictionary":
                if identity < 8 or name in factory.values():
                    raise ValueError("Invalid user-owned dictionary identity")
                validate_name(name)
            if any(row[64:96]) or bool(marker) != (base != 65535) or member:
                raise ValueError("Invalid user-owned snapshot descriptor")
        if base == 65535:
            if member:
                raise ValueError("Unexpected compiled member for a journal dictionary")
            continue
        if base >= len(snapshot.extents) or snapshot.extents[base][4] != 1:
            raise ValueError("Invalid compact image reference")
        members = references.setdefault(base, set())
        if member in members:
            raise ValueError("Duplicate compiled image member")
        members.add(member)
        start, _, size, _, _, _ = snapshot.extents[base]
        address = start * SEGMENT
        blob = image[address:address + size]
        if len(blob) < 40:
            raise ValueError("Truncated compact image")
        count = struct.unpack_from("<H", blob, 4)[0]
        if not 1 <= count <= 64 or member >= count or (marker and count != 1):
            raise ValueError("Invalid compact image member count")
        counts[base] = count
        if marker:
            validate_search_index(blob, XIP + address)
        # Walk the compiled data using the same host reader as export.
        from jsc4 import reader
        lookup = reader(blob, XIP + address, member, count)
        entries = dict(compact_entries(blob, XIP + address, member, count))
        if any(lookup(outline) != text for outline, text in entries.items()):
            raise ValueError("Compact recovery image failed lookup verification")
    if not (factory.keys() <= ids and "user_dictionary" in names):
        raise ValueError("Recovery bundle is missing a pinned or personal dictionary")
    if any(members != set(range(counts[base])) for base, members in references.items()):
        raise ValueError("Recovery bundle is missing a compact image member")
    if set(references) != {i for i, extent in enumerate(snapshot.extents) if extent[4] == 1}:
        raise ValueError("Unreferenced compact extent in recovery bundle")


def reconstruct(backup):
    """Return a pair of coherent roots for offline validation only."""
    if (not isinstance(backup, dict) or backup.get("format") != FORMAT
            or backup.get("host_layout") not in LAYOUTS or backup.get("sha256") != digest(backup)):
        raise ValueError("Recovery bundle version, host layout or checksum is invalid")
    payload = unpack(backup.get("catalog"))
    extents = descriptors(payload)
    chunks = backup.get("extents")
    if not isinstance(chunks, list) or len(chunks) != len(extents):
        raise ValueError("Recovery bundle is missing an extent")
    image = bytearray(b"\xff" * LIMIT)
    for (start, _, size, crc, kind, _), data in zip(extents, chunks):
        blob = unpack(data, size)
        if kind != 2 and zlib.crc32(blob) != crc:
            raise ValueError("Recovery extent checksum differs from its catalog")
        address = start * SEGMENT
        image[address:address + size] = blob
    for slot in range(2):
        start = slot * SEGMENT
        image[start:start + 32 + len(payload)] = catalog_header(slot + 1, payload) + payload

    def read(address, size):
        return bytes(image[address:address + size])

    snapshot = Snapshot(read, 32, len(payload), zlib.crc32(payload), strict=True)
    validate_rows(snapshot, image)
    return bytes(image), snapshot


def save_atomic(path, document, dump=json.dump):
    """Publish only a flushed, complete file and refuse to replace any backup."""
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            dump(document, output, ensure_ascii=False, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
            os.link(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Verify a complete experimental backup offline.")
    parser.add_argument("backup", type=Path)
    args = parser.parse_args()
    backup = json.loads(args.backup.read_text(encoding="utf-8"))
    _, snapshot = reconstruct(backup)
    print(f"Verified {len(snapshot.rows)} dictionaries, {len(snapshot.entries):,} live overlay records"
          f" and {len(snapshot.extents)} extents. Host layout: {backup['host_layout']}.")


if __name__ == "__main__":
    main()
