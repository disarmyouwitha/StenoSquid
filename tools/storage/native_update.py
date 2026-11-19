"""Build a copy-on-write Native update from a frozen catalog and its journals."""

from dataclasses import dataclass
import struct
import zlib

from build_native_image import SEGMENT, write_journal_header

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
    def __init__(self, read, catalog_address, catalog_bytes, crc):
        payload = read(catalog_address, catalog_bytes)
        if len(payload) != catalog_bytes or zlib.crc32(payload) != crc:
            raise ValueError("Catalog changed or failed its checksum")
        self.next_id, count, extents, through = struct.unpack_from("<IHHQ", payload)
        rows_end = 32 + count * 96
        if count > 64 or extents > 253 or len(payload) != rows_end + extents * 24:
            raise ValueError("Invalid source catalog shape")
        self.rows = [payload[32 + i * 96:32 + (i + 1) * 96] for i in range(count)]
        self.extents = list(EXTENT.iter_unpack(payload[rows_end:]))
        identities = {struct.unpack_from("<I", row)[0] for row in self.rows}
        personal = [row for row in self.rows if row[16:16 + row[12]] == b"user_dictionary"]
        if len(personal) != 1 or personal[0][8:10] != b"\xff\xff":
            raise ValueError("Expected one overlay-only personal dictionary")
        self.personal = struct.unpack_from("<I", personal[0])[0]
        self.journals = [item for item in self.extents if item[4] == 2]
        self.highwater = 0
        self.anchor = None
        self.used = {}
        records, tail = {}, []
        for segment, _, _, _, _, epoch in self.journals:
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
                offset += entry.span
            self.used[segment] = offset - 256

        # The forward checkpoint is authoritative. A Reset can have been
        # checkpointed away along with its journal, so replaying all history
        # from retained segments could wrongly resurrect an old Set.
        self.entries = {}
        for start, _, length, expected, kind, _ in self.extents:
            if kind != 3:
                continue
            data = read(start * SEGMENT, length)
            if len(data) != length or zlib.crc32(data) != expected:
                raise ValueError("Source forward index failed checksum")
            for (address,) in struct.iter_unpack("<I", data):
                entry = records.get(address)
                if not entry or entry.operation == 2:
                    raise ValueError("Invalid source forward index record")
                if entry.identity in identities:
                    self.entries[entry.key] = entry
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


def plan(snapshot, protected, incoming=None):
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
            key = snapshot.personal, strokes
            old = entries.get(key)
            if old is None or old.operation or old.text != text:
                pending.append(Edit(0, snapshot.personal, 0, strokes, text))
        if not pending:
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
    for kind, records in ((3, sorted(entries.values(), key=lambda entry: entry.key)),
                          (4, sorted((entry for entry in entries.values() if not entry.operation),
                                     key=lambda entry: (entry.text, entry.key)))):
        if not records:
            continue
        data = b"".join(struct.pack("<I", entry.address) for entry in records)
        count = (len(data) + SEGMENT - 1) // SEGMENT
        start = allocate(count)
        for index in range(count):
            segments[start + index] = data[index * SEGMENT:(index + 1) * SEGMENT]
        extents.append(EXTENT.pack(start, count, len(data), zlib.crc32(data), kind, 0))
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
    payload = struct.pack("<IHHQ16x", snapshot.next_id, len(rows), len(extents), highwater)
    payload += b"".join(rows) + b"".join(extents)
    segments[metadata] = payload
    return Update(segments, metadata, payload, len(victims), len(pending))


def transfer(console, incoming=None, progress=print):
    """Preflight, upload and commit one update over USB HID."""
    import base64

    def command(text, timeout=30):
        reply = console.command("native_install update " + text, timeout=timeout)
        if reply != "OK" and not reply.startswith("OK "):
            raise RuntimeError(reply)
        return reply

    mode = "compact" if incoming is None else "import"
    answer = command("begin " + mode)
    try:
        _, address, length, checksum, ownership = answer.split()
        if len(ownership) != 64:
            raise ValueError("Invalid ownership bitmap")
        words = [int(ownership[i:i + 8], 16) for i in range(0, 64, 8)]
        protected = {i for i in range(256) if words[i // 32] & (1 << (i % 32))}

        def read(start, size):
            result = bytearray()
            for offset in range(0, size, 512):
                count = min(size - offset, 512)
                data = base64.b64decode(command(f"read {start + offset} {count}")[3:], validate=True)
                if len(data) != count:
                    raise RuntimeError("Short snapshot read")
                result.extend(data)
            return bytes(result)

        progress("Reading the current dictionary store and planning free space…")
        snapshot = Snapshot(read, int(address), int(length), int(checksum))
        update = plan(snapshot, protected, incoming)
        if update is None:
            command("abort")
            progress("No journals can be reclaimed in this pass." if incoming is None
                     else "All imported entries already match.")
            return False
        for index, (segment, block) in enumerate(update.segments.items(), 1):
            command(f"erase {segment}")
            for offset in range(0, len(block), 512):
                data = block[offset:offset + 512]
                if all(byte == 255 for byte in data):
                    continue
                encoded = base64.b64encode(data).decode("ascii")
                command(f"write {segment * SEGMENT + offset} {encoded}")
            if index % 8 == 0 or index == len(update.segments):
                progress(f"Staged {index}/{len(update.segments)} segments")
        progress("Validating the staged dictionaries and publishing the new catalog…")
        reply = command(f"commit {update.metadata} {len(update.payload)} {zlib.crc32(update.payload)}",
                        timeout=1800)
        if reply != "OK Rebooting":
            raise RuntimeError("Unexpected commit response")
        progress(f"Published {update.changes:,} entries; the left half is rebooting."
                 if incoming is not None else
                 f"Reclaimed {update.reclaimed} old journals; the left half is rebooting.")
        return True
    except BaseException:
        try:
            command("abort", timeout=2)
        except (OSError, RuntimeError):
            pass
        raise
