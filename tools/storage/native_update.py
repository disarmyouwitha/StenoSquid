"""Build a copy-on-write Native update from a frozen catalog and its journals."""

from dataclasses import dataclass
import hashlib
import re
import struct
import zlib

from build_native_image import OUTLINE_FIRST, SEGMENT, write_journal_header

EXTENT = struct.Struct("<HHIIIQ")
RECORD = struct.Struct("<4sIQIIII")
KEY = struct.Struct("<IBBH")
COMMIT = 0x434F4D54


class NoSpace(ValueError):
    """The old and new versions cannot fit together."""


@dataclass(frozen=True)
class Edit:
    address: int
    identity: int
    operation: int
    strokes: tuple
    text: bytes

    @property
    def key(self):
        return self.identity, self.strokes

    @property
    def outline_key(self):
        return self.strokes, self.identity

    @property
    def span(self):
        return (43 + 4 * len(self.strokes) + len(self.text)) & ~3


def read_record(data, offset, start):
    magic, span, sequence, size, crc, header_crc, commit = RECORD.unpack_from(data, offset)
    if span != (35 + size) & ~3 or offset + span > SEGMENT or size < 12 or not sequence:
        raise ValueError("Invalid staged-source record span")
    body = data[offset + 32:offset + 32 + size]
    if (magic != b"SQJR" or commit != COMMIT or zlib.crc32(data[offset:offset + 24]) != header_crc
            or zlib.crc32(body, zlib.crc32(struct.pack("<Q", sequence))) != crc):
        raise ValueError("Invalid staged-source record checksum")
    identity, operation, count, length = KEY.unpack_from(body)
    if not identity or operation > 2 or not 1 <= count <= 16 or length > 2047:
        raise ValueError("Invalid staged-source record key")
    if size != 8 + 4 * count + length or (operation and length):
        raise ValueError("Invalid staged-source record length")
    strokes = struct.unpack_from(f"<{count}I", body, 8)
    text = body[8 + 4 * count:]
    if any(not 0 < stroke < 1 << 25 for stroke in strokes) or b"\0" in text:
        raise ValueError("Invalid staged-source translation")
    text.decode("utf-8")
    return Edit(start + offset, identity, operation, strokes, text), sequence


def encode_record(edit, sequence):
    payload = KEY.pack(edit.identity, edit.operation, len(edit.strokes), len(edit.text))
    payload += struct.pack(f"<{len(edit.strokes)}I", *edit.strokes) + edit.text
    crc = zlib.crc32(payload, zlib.crc32(struct.pack("<Q", sequence)))
    header = struct.pack("<4sIQII", b"SQJR", edit.span, sequence, len(payload), crc)
    return header + struct.pack("<II", zlib.crc32(header), COMMIT) + payload


class Snapshot:
    def __init__(self, read, catalog_address, catalog_bytes, crc, *, metadata_only=False, strict=False):
        payload = read(catalog_address, catalog_bytes)
        if len(payload) != catalog_bytes or zlib.crc32(payload) != crc:
            raise ValueError("Catalog changed or failed its checksum")
        self.payload = payload
        self.next_id, count, extents, through = struct.unpack_from("<IHHQ", payload)
        rows_end = 32 + count * 96
        if count > 64 or extents > 253 or len(payload) != rows_end + extents * 24:
            raise ValueError("Invalid source catalog shape")
        self.rows = [payload[32 + i * 96:32 + (i + 1) * 96] for i in range(count)]
        self.extents = list(EXTENT.iter_unpack(payload[rows_end:]))
        identities = {struct.unpack_from("<I", row)[0] for row in self.rows}
        personal = [row for row in self.rows if row[16:16 + row[12]] == b"user_dictionary"]
        if len(personal) != 1 or (personal[0][8:10] != b"\xff\xff" and personal[0][11] != 1):
            raise ValueError("Expected one personal dictionary")
        self.personal = struct.unpack_from("<I", personal[0])[0]
        for _, _, _, _, kind, auxiliary in self.extents:
            if kind == 3 and auxiliary != OUTLINE_FIRST:
                raise ValueError("Unsupported forward journal checkpoint. Reprovision with current firmware and Manager.")
            if kind == 4 and auxiliary:
                raise ValueError("Unsupported reverse journal checkpoint")
        if metadata_only:
            return
        self.journals = [item for item in self.extents if item[4] == 2]
        self.highwater = 0
        self.anchor = None
        self.used = {}
        records, tail = {}, []
        epochs = 0
        boundary = not through
        sequences = {}
        for segment, _, _, _, _, epoch in self.journals:
            if strict and epoch <= epochs:
                raise ValueError("Recovery journal epochs are out of order")
            epochs = epoch
            start = segment * SEGMENT
            data = read(start, SEGMENT)
            if len(data) != SEGMENT or data[:16] != struct.pack("<8sQ", b"SQJSEG01", epoch):
                raise ValueError("Invalid source journal header")
            if zlib.crc32(data[:16]) != struct.unpack_from("<I", data, 16)[0]:
                raise ValueError("Invalid source journal checksum")
            offset = 256
            while offset + RECORD.size <= SEGMENT:
                if struct.unpack_from("<I", data, offset + 28)[0] != COMMIT:
                    break
                entry, sequence = read_record(data, offset, start)
                if sequence <= self.highwater:
                    raise ValueError("Source journal sequence is out of order")
                self.highwater, self.anchor = sequence, segment
                if sequence > through:
                    tail.append(entry)
                records[entry.address] = entry
                if strict:
                    sequences[entry.address] = sequence
                    boundary |= sequence == through
                offset += entry.span
            self.used[segment] = offset - 256

        # The forward checkpoint is authoritative. A Reset can have been
        # checkpointed away along with its journal, so replaying all history
        # from retained segments could wrongly resurrect an old Set.
        self.entries = {}
        if strict and not boundary:
            raise ValueError("Recovery checkpoint boundary is missing")
        indexed = {}
        for start, _, length, expected, kind, _ in self.extents:
            if kind != 3 and (not strict or kind != 4):
                continue
            data = read(start * SEGMENT, length)
            if len(data) != length or zlib.crc32(data) != expected:
                raise ValueError("Source forward index failed checksum")
            previous = None
            for (address,) in struct.iter_unpack("<I", data):
                entry = records.get(address)
                if not entry or entry.operation == 2:
                    raise ValueError("Invalid source index record")
                if strict:
                    sequence = sequences[address]
                    if entry.identity >= self.next_id or sequence > through or (kind == 4 and entry.operation):
                        raise ValueError("Invalid recovery index record")
                    key = entry.outline_key if kind == 3 else (entry.text, entry.key)
                    if previous is not None and key <= previous:
                        raise ValueError("Recovery index is out of order")
                    previous = key
                    indexed.setdefault(kind, set()).add(address)
                if kind == 3 and entry.identity in identities:
                    self.entries[entry.key] = entry
        if strict and {address for address in indexed.get(3, ()) if not records[address].operation} != indexed.get(4, set()):
            raise ValueError("Recovery indexes disagree")
        for entry in tail:
            if entry.identity not in identities:
                continue
            if entry.operation == 2:
                self.entries.pop(entry.key, None)
            else:
                self.entries[entry.key] = entry
        if through > self.highwater or len(tail) > 256:
            raise ValueError("Source journal checkpoint is inconsistent")


@dataclass
class Update:
    segments: dict
    metadata: int
    payload: bytes
    reclaimed: int
    changes: int


def row_name(row):
    return row[16:16 + row[12]].decode("utf-8")


def validate_name(name):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,43}", name) or name.endswith(" "):
        raise ValueError("Use 1–44 letters, digits, spaces, dots, underscores or hyphens for the name")
    return name


def metadata_payload(snapshot, *, move=None, rename=None, delete=None):
    rows = snapshot.rows.copy()
    names = [row_name(row) for row in rows]
    target = move[0] if move else rename[0] if rename else delete
    if target not in names:
        raise ValueError(f"No installed dictionary named {target!r}")
    index = names.index(target)
    if move:
        position = move[1] - 1
        if not 0 <= position < len(rows):
            raise ValueError("Choose a position in the current priority list")
        if index == position:
            return None
        rows.insert(position, rows.pop(index))
    else:
        if target == "user_dictionary" or rows[index][11] == 2 or (rows[index][8:10] != b"\xff\xff" and not (rename and rows[index][11] == 1)):
            raise ValueError("Only independent named dictionaries can be renamed or deleted")
        if rename:
            label = validate_name(rename[1])
            if label == target:
                return None
            if label in names:
                raise ValueError(f"A dictionary named {label!r} is already installed")
            row = bytearray(rows[index])
            row[12] = len(label)
            row[16:60] = label.encode("ascii").ljust(44, b"\0")
            rows[index] = row
        else:
            rows.pop(index)
    prefix = bytearray(snapshot.payload[:32])
    struct.pack_into("<H", prefix, 4, len(rows))
    return bytes(prefix) + b"".join(rows) + snapshot.payload[32 + len(snapshot.rows) * 96:]


def plan(snapshot, protected, incoming=None, *, name=None, limit=64, move=None, rename=None, delete=None):
    """Stage replacements in free segments. None selects live-record cleaning."""
    free = set(range(2, 255)) - set(protected)
    segments = {}

    def allocate(count=1):
        if len(free) - count >= 3:
            for start in sorted(free):
                block = range(start, start + count)
                if all(segment in free for segment in block):
                    free.difference_update(block)
                    return start
        raise NoSpace("Not enough contiguous staging space; no data was changed")

    if move or rename or delete:
        payload = metadata_payload(snapshot, move=move, rename=rename, delete=delete)
        if payload is None:
            return None
        metadata = allocate()
        return Update({metadata: payload}, metadata, payload, 0, 0)

    identity, next_id = snapshot.personal, snapshot.next_id
    if name is not None:
        validate_name(name)
        if any(row_name(row) == name for row in snapshot.rows):
            raise ValueError(f"A dictionary named {name!r} is already installed")
        if len(snapshot.rows) >= limit or next_id == 0xffffffff:
            raise ValueError("The keyboard has reached its dictionary limit")
        identity, next_id = next_id, next_id + 1

    entries = snapshot.entries.copy()
    pending = []
    victims = set()
    if incoming is None:
        live_bytes = {segment: 0 for segment in snapshot.used}
        for entry in entries.values():
            live_bytes[entry.address // SEGMENT] += entry.span
        anchor = {snapshot.anchor}
        if snapshot.journals:
            anchor.add(snapshot.journals[-1][0])
        candidates = sorted(
            (segment for segment, used in snapshot.used.items()
             if segment not in anchor and used > live_bytes[segment]),
            key=lambda segment: (live_bytes[segment], segment),
        )
        # One explicit incremental pass. Copy at most one journal worth of
        # survivors and release at least two old journals when a copy is needed.
        remaining = SEGMENT - 256
        for segment in candidates:
            if live_bytes[segment] <= remaining:
                victims.add(segment)
                remaining -= live_bytes[segment]
        if len(victims) <= int(remaining != SEGMENT - 256):
            return None
        pending = [entry for entry in entries.values() if entry.address // SEGMENT in victims]
    else:
        for strokes, text in incoming.items():
            key = identity, strokes
            old = entries.get(key)
            if old is None or old.operation or old.text != text:
                pending.append(Edit(0, identity, 0, strokes, text))
        if not pending and name is None:
            return None

    extents = []
    remap = {}
    for original, extent in enumerate(snapshot.extents):
        if extent[4] == 1 or (extent[4] == 2 and extent[0] not in victims):
            remap[original] = len(extents)
            extents.append(EXTENT.pack(*extent))
    highwater = snapshot.highwater
    epoch = max((item[5] for item in snapshot.journals), default=0)
    offset = SEGMENT
    for entry in pending:
        if offset + entry.span > SEGMENT:
            segment = allocate()
            block = bytearray(b"\xff" * SEGMENT)
            segments[segment] = block
            epoch += 1
            extents.append(write_journal_header(block, 0, epoch, extent_segment=segment))
            offset = 256
        highwater += 1
        record = encode_record(entry, highwater)
        block[offset:offset + len(record)] = record
        entries[entry.key] = Edit(segment * SEGMENT + offset, entry.identity, entry.operation,
                                  entry.strokes, entry.text)
        offset += entry.span
    for kind, records in ((3, sorted(entries.values(), key=lambda entry: entry.outline_key)),
                          (4, sorted((entry for entry in entries.values() if not entry.operation),
                                     key=lambda entry: (entry.text, entry.key)))):
        if not records:
            continue
        data = b"".join(struct.pack("<I", entry.address) for entry in records)
        count = (len(data) + SEGMENT - 1) // SEGMENT
        start = allocate(count)
        for index in range(count):
            segments[start + index] = data[index * SEGMENT:(index + 1) * SEGMENT]
        extents.append(EXTENT.pack(start, count, len(data), zlib.crc32(data), kind,
                                   OUTLINE_FIRST if kind == 3 else 0))
    metadata = allocate()
    if len(extents) > 253:
        raise NoSpace("Catalog extent limit; no data was changed")
    rows = []
    for original in snapshot.rows:
        row = bytearray(original)
        base = struct.unpack_from("<H", row, 8)[0]
        if base != 65535:
            struct.pack_into("<H", row, 8, remap[base])
        rows.append(row)
    if name is not None:
        label = name.encode("ascii")
        row = bytearray(96)
        struct.pack_into("<IIH", row, 0, identity, 1, 65535)
        row[12] = len(label)
        row[16:16 + len(label)] = label
        personal_position = next(i for i, row in enumerate(rows)
                                 if struct.unpack_from("<I", row)[0] == snapshot.personal)
        rows.insert(personal_position + 1, row)
    payload = struct.pack("<IHHQ16x", next_id, len(rows), len(extents), highwater)
    payload += b"".join(rows) + b"".join(extents)
    segments[metadata] = payload
    return Update(segments, metadata, payload, len(victims), len(pending))


def update_command(console, text, timeout=30):
    reply = console.command("native_install update " + text, timeout=timeout)
    if reply != "OK" and not reply.startswith("OK "):
        raise RuntimeError(reply)
    return reply


def read_snapshot(console, answer, *, metadata_only=False, on_read=None, trim_journals=False):
    _, address, length, checksum, ownership = answer.split()
    if len(ownership) != 64:
        raise ValueError("Invalid ownership bitmap")
    words = [int(ownership[i:i + 8], 16) for i in range(0, 64, 8)]
    protected = {i for i in range(256) if words[i // 32] & (1 << (i % 32))}

    address, length, checksum = int(address), int(length), int(checksum)
    catalog = None
    journals = set()

    def read(start, size):
        nonlocal catalog
        if trim_journals and (start, size) == (address, length):
            if catalog is None:
                catalog = read_flash(console, start, size, on_read=on_read)
            return catalog
        if trim_journals and start in journals and size == SEGMENT:
            return read_live_journal(console, start, on_read=on_read)
        return read_flash(console, start, size, on_read=on_read)

    if trim_journals and not metadata_only:
        metadata = Snapshot(read, address, length, checksum, metadata_only=True)
        journals = {segment * SEGMENT for segment, _, _, _, kind, _ in metadata.extents if kind == 2}
    return Snapshot(read, address, length, checksum, metadata_only=metadata_only), protected


def read_live_journal(console, start, *, on_read=None):
    """Read only through the first unpublished record of a frozen journal.

    Snapshot's record scanner stops at the same marker. Padding the unread tail
    is for logical editing only; exact recovery backups always use full reads.
    """
    data = bytearray()

    def ensure(end):
        while len(data) < end:
            size = min(512, SEGMENT - len(data))
            data.extend(read_flash(console, start + len(data), size, on_read=on_read))

    ensure(512)
    if data[:8] == b"SQJSEG01":
        offset = 256
        while offset + RECORD.size <= SEGMENT:
            ensure(offset + RECORD.size)
            if struct.unpack_from("<I", data, offset + 28)[0] != COMMIT:
                break
            span = struct.unpack_from("<I", data, offset + 4)[0]
            if span < 44 or span % 4 or offset + span > SEGMENT:
                break  # Snapshot will report the malformed committed record.
            ensure(offset + span)
            offset += span
    return bytes(data).ljust(SEGMENT, b"\xff")


def read_flash(console, start, size, *, on_read=None):
    import base64

    result = bytearray()
    for offset in range(0, size, 512):
        count = min(size - offset, 512)
        reply = update_command(console, f"read {start + offset} {count}")
        data = base64.b64decode(reply[3:], validate=True)
        if len(data) != count:
            raise RuntimeError("Short snapshot read")
        result.extend(data)
        if on_read is not None:
            on_read(count)
    return bytes(result)


def backup_all(console, *, on_progress=None):
    """Export the catalog and exact referenced extents from one frozen view."""
    from native_recovery import capture, digest

    layout = console.command("get_parameter host_layout")
    answer = update_command(console, "begin backup")
    try:
        snapshot, _ = read_snapshot(console, answer, metadata_only=True)
        total = sum(size for _, _, size, *_ in snapshot.extents)
        reporting = on_progress is not None and total > 0
        received = last_percent = 0
        if reporting:
            on_progress(0, total)

        def count_bytes(count):
            nonlocal received, last_percent
            received += count
            percent = received * 100 // total
            if percent != last_percent:
                last_percent = percent
                on_progress(received, total)

        def read(start, size):
            return read_flash(console, start, size, on_read=count_bytes if reporting else None)

        result = capture(read, snapshot, layout)
        if console.command("get_parameter host_layout") != layout:
            raise RuntimeError("Host layout changed during backup. Retry the backup.")
        if serial := getattr(console, "serial", None):
            from datetime import datetime, timezone

            status = console.command("native_install status").split()
            if len(status) != 3 or status[:2] != ["NATIVE2", "READY"] or not status[2].isdigit():
                raise RuntimeError("Could not read the captured catalog generation.")
            result["capture"] = dict(serial=serial, generation=int(status[2]),
                                     timestamp=datetime.now(timezone.utc).isoformat())
            result["sha256"] = digest(result)
        return result
    finally:
        update_command(console, "abort", timeout=5)


def overlay_usage(console, *, metadata_only=False):
    capability = console.command("native_install update journal-usage-capabilities")
    if capability == "ERR Native flash busy":
        raise RuntimeError(capability)
    on_device = capability == "OK USAGE2"
    if metadata_only and not on_device:
        raise RuntimeError("This keyboard does not offer a metadata-only journal measurement.")
    answer = update_command(console, "begin metadata" if on_device else "begin backup")
    try:
        snapshot, _ = read_snapshot(console, answer, metadata_only=on_device)
        usage = {struct.unpack_from("<I", row)[0]: {"count": 0, "entries": 0, "bytes": 0, "blocks": {}}
                 for row in snapshot.rows}

        def add(segment, identity, count, entries, bytes_used):
            stats = usage[identity]
            stats["count"] += count
            stats["entries"] += entries
            stats["bytes"] += bytes_used
            blocks = stats["blocks"]
            blocks[segment] = blocks.get(segment, 0) + bytes_used

        if on_device:
            journals = {segment for start, span, _, _, kind, _ in snapshot.extents if kind == 2
                        for segment in range(start, start + span)}
            position, total = 0, None
            while total is None or position < total:
                marker, version, cursor, limit, *rows = update_command(console, f"journal-usage {position}").split()
                cursor, limit = int(cursor), int(limit)
                if (marker != "OK" or version != "USAGE2" or not position <= cursor <= limit
                        or cursor - position > 128 or (total is not None and total != limit)
                        or (cursor == position and cursor != limit)):
                    raise ValueError("Invalid journal usage cursor")
                counts = 0
                seen = set()
                for row in rows:
                    segment, identity, count, entries, bytes_used = map(int, row.split(","))
                    pair = segment, identity
                    if (segment not in journals or identity not in usage or pair in seen or not 0 <= entries <= count
                            or not 0 < count <= cursor - position or not 0 < bytes_used <= SEGMENT):
                        raise ValueError("Invalid journal usage totals")
                    seen.add(pair)
                    counts += count
                    add(segment, identity, count, entries, bytes_used)
                if counts > cursor - position:
                    raise ValueError("Invalid journal usage count")
                position, total = cursor, limit
        else:
            for entry in snapshot.entries.values():
                add(entry.address // SEGMENT, entry.identity, 1, entry.operation == 0, entry.span)
        return usage
    finally:
        update_command(console, "abort", timeout=5)


def export_dictionary(console, name, *, on_read=None, on_snapshot=None, trim_journals=False):
    """Read one logical dictionary, including overrides on a packed base."""
    answer = update_command(console, "begin backup")
    try:
        snapshot, _ = read_snapshot(console, answer, on_read=on_read, trim_journals=trim_journals)
        if on_snapshot is not None:
            on_snapshot(snapshot)
        return effective_entries(snapshot, name, lambda at, n: read_flash(console, at, n, on_read=on_read))
    finally:
        update_command(console, "abort", timeout=5)


def effective_entries(snapshot, name, read):
    from build_native_image import XIP
    from hybrid import compact_entries, format_stroke
    from jsc4 import require_snapshot_hint

    row = next((row for row in snapshot.rows if row_name(row) == name), None)
    if row is None:
        raise ValueError(f"No installed dictionary named {name!r}")
    identity, _, base, member = struct.unpack_from("<IIHB", row)
    entries = {}
    if base != 65535:
        segment, _, size, checksum, _, _ = snapshot.extents[base]
        address = segment * SEGMENT
        blob = read(address, size)
        if zlib.crc32(blob) != checksum:
            raise ValueError("Compiled dictionary checksum failed during export.")
        if row[11]:
            require_snapshot_hint(blob, XIP + address)
        count = struct.unpack_from("<H", blob, 4)[0]
        entries.update(compact_entries(blob, XIP + address, member, count))
    for entry in snapshot.entries.values():
        if entry.identity != identity:
            continue
        outline = "/".join(map(format_stroke, entry.strokes))
        if entry.operation:
            entries.pop(outline, None)
        else:
            entries[outline] = entry.text.decode("utf-8")
    return entries


def publish(console, update, progress):
    import base64
    import native_transfer

    try:
        native_transfer.upload(console, update, progress)
        return
    except native_transfer.TransferUnavailable:
        pass  # Not advertised, or refused before any data moved: use the Base64 console path.

    total = sum(map(len, update.segments.values()))
    processed = 0
    last_percent = 0
    progress("Writing dictionary data… 0%")
    for segment, block in update.segments.items():
        update_command(console, f"erase {segment}")
        for offset in range(0, len(block), 512):
            data = block[offset:offset + 512]
            if data != b"\xff" * len(data):
                encoded = base64.b64encode(data).decode("ascii")
                update_command(console, f"write {segment * SEGMENT + offset} {encoded}")
            processed += len(data)
            percent = processed * 100 // total
            if percent >= last_percent + 5 or percent == 100:
                progress(f"Writing dictionary data… {percent}%")
                last_percent = percent
    progress("Verifying staged data and saving the catalog…")
    reply = update_command(console, f"commit {update.metadata} {len(update.payload)} {zlib.crc32(update.payload)}",
                           timeout=1800)
    if reply != "OK Rebooting":
        raise RuntimeError("Unexpected commit response")


def abort(console):
    try:
        update_command(console, "abort", timeout=2)
    except (OSError, RuntimeError):
        pass


def transfer(console, incoming=None, progress=print, *, move=None, rename=None, delete=None,
             preview_compact=False, expected_compact=None):
    """Preflight, upload and commit one update over the HID console."""
    def command(text, timeout=30):
        return update_command(console, text, timeout)

    if move:
        mode = "reorder"
    elif rename:
        mode = "rename"
    elif delete:
        mode = "delete"
    else:
        mode = "compact" if incoming is None else "import"
    if (preview_compact or expected_compact is not None) and mode != "compact":
        raise ValueError("A journal preview only applies to compaction")
    answer = command("begin " + mode)
    try:
        progress("Reading the current dictionary store and planning free space…")
        snapshot, protected = read_snapshot(console, answer, metadata_only=bool(move or rename or delete))
        update = plan(snapshot, protected, incoming, move=move, rename=rename, delete=delete)
        if preview_compact or expected_compact is not None:
            token = None
            victims = []
            if update is not None:
                _, rows, _ = struct.unpack_from("<IHH", update.payload)
                next_journals = {extent[0] for extent in EXTENT.iter_unpack(update.payload[32 + 96 * rows:])
                                 if extent[4] == 2}
                victims = sorted(extent[0] for extent in snapshot.journals if extent[0] not in next_journals)
                digest = hashlib.sha256(console.serial.encode() + answer.encode() + snapshot.payload)
                digest.update(struct.pack("<Q", snapshot.highwater))
                for segment, block in sorted(update.segments.items()):
                    digest.update(struct.pack("<II", segment, len(block)))
                    digest.update(block)
                token = digest.hexdigest()
            if preview_compact:
                command("abort")
                return {"token": token, "reclaimable": victims}
            if token != expected_compact:
                raise ValueError("Journal contents or allocation changed. Preview cleanup again.")
        if update is None:
            command("abort")
            if move:
                progress("The dictionary is already at that priority.")
            elif rename:
                progress("The dictionary already has that name.")
            elif incoming is None:
                progress("No journals can be reclaimed in this pass.")
            else:
                progress("All imported entries already match.")
            return False
        publish(console, update, progress)
        if move:
            progress(f"Moved {move[0]} to priority {move[1]}; the left half is rebooting.")
        elif rename:
            progress(f"Renamed {rename[0]} to {rename[1]}; the left half is rebooting.")
        elif delete:
            progress(f"Deleted {delete}; the left half is rebooting.")
        elif incoming is not None:
            progress(f"Published {update.changes:,} entries; the left half is rebooting.")
        else:
            progress(f"Reclaimed {update.reclaimed} old journals; the left half is rebooting.")
        return True
    except BaseException:
        abort(console)
        raise
