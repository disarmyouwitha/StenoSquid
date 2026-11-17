"""Build the personal dictionary import image: record segments and both hash tables.

The format is docs/personal-dictionary-spec.md section 3, exactly as
Store::ImportFinish in src/steno_squid/user_dictionary_log.h checks it.

The build takes two steps, because slots hold physical segment numbers and
record checksums are seeded with segment sequence numbers, and both come from
the keyboard's reply to `user_dictionary_import begin`:

    image = Image(entries)            # {tuple of stroke words: UTF-8 bytes}
    # send: begin image.record_segments image.forward_segments image.reverse_segments
    segments = image.build(first_sequence, fwd_epoch, rev_epoch, physical_segments)
    # send segments[n] as plan segment n, from offset 0
"""

import struct
import zlib

SEG_BYTES = 0x10000
SEG_HEADER = 16
SLOTS_PER_SEG = (SEG_BYTES - SEG_HEADER) // 4
MAX_TABLE_SEGS = 32
MAX_STROKES = 16
MAX_TEXT = 2047
MAGIC = 0x3355445A
EMPTY = 0xFFFFFFFF
RECORD, FORWARD, REVERSE = 1, 2, 3
# ALLOC and COMMIT clear, DEAD set: a live record.
LIVE = 1 << 29


def hash_bytes(data):
    """The firmware's FNV-1a."""
    value = 2166136261
    for byte in data:
        value = ((value ^ byte) * 16777619) & 0xFFFFFFFF
    return value


def record_bytes(strokes, text_bytes):
    return 8 + 4 * strokes + ((text_bytes + 3) & ~3)


def size_check(strokes, text_bytes):
    return hash_bytes(struct.pack("<II", strokes, text_bytes)) & 0xFFF


def table_segments(count):
    """Segments for a table at most half full, within the format's limits."""
    return max(1, min(MAX_TABLE_SEGS, -(-2 * count // SLOTS_PER_SEG)))


def segment_header(kind, index, epoch, sequence):
    head = struct.pack("<IBBHI", MAGIC, kind, index, epoch, sequence)
    return head + struct.pack("<I", zlib.crc32(head))


def used_part(segment):
    """A segment without its erased tail, which the keyboard need not be sent."""
    used = len(segment.rstrip(b"\xff"))
    return bytes(segment[:max(SEG_HEADER, (used + 3) & ~3)])


class Image:
    def __init__(self, entries):
        if not entries:
            raise ValueError("An import needs at least one entry.")
        if len(entries) >= MAX_TABLE_SEGS * SLOTS_PER_SEG:
            raise ValueError(f"{len(entries):,} entries do not fit the lookup tables.")
        # Records are packed in order and never cross a segment boundary.
        self.layout = [[]]
        offset = SEG_HEADER
        for strokes, text in entries.items():
            if not 1 <= len(strokes) <= MAX_STROKES or len(text) > MAX_TEXT:
                raise ValueError("An entry needs 1 to 16 strokes and at most 2,047 text bytes.")
            size = record_bytes(len(strokes), len(text))
            if offset + size > SEG_BYTES:
                self.layout.append([])
                offset = SEG_HEADER
            self.layout[-1].append((offset, struct.pack(f"<{len(strokes)}I", *strokes), bytes(text)))
            offset += size
        self.count = len(entries)
        self.record_segments = len(self.layout)
        self.forward_segments = self.reverse_segments = table_segments(self.count)
        self.total_segments = self.record_segments + self.forward_segments + self.reverse_segments

    def build(self, first_sequence, fwd_epoch, rev_epoch, physical):
        """The segments in plan order: records, forward table, reverse table.

        `physical` lists the keyboard's segment number for each plan position.
        """
        if len(physical) != self.total_segments or any(not 0 <= s < 256 for s in physical):
            raise ValueError("The keyboard's segment plan does not match the image.")
        tables = [[EMPTY] * (self.forward_segments * SLOTS_PER_SEG),
                  [EMPTY] * (self.reverse_segments * SLOTS_PER_SEG)]
        segments = []
        for n, records in enumerate(self.layout):
            sequence = (first_sequence + n) & 0xFFFFFFFF
            segment = bytearray(segment_header(RECORD, 0, 0, sequence))
            for offset, key, text in records:
                assert offset == len(segment)
                strokes = len(key) // 4
                word = LIVE | ((strokes - 1) << 24) | (len(text) << 12) | size_check(strokes, len(text))
                # The checksum is seeded with the segment's sequence number.
                segment += struct.pack("<II", word, zlib.crc32(key + text, sequence))
                segment += key + text + b"\0" * (-len(text) % 4)
                for table, hashed in zip(tables, (hash_bytes(key), hash_bytes(text))):
                    slot = hashed % len(table)
                    while table[slot] != EMPTY:
                        slot = (slot + 1) % len(table)
                    table[slot] = (hashed >> 22 << 22) | (physical[n] << 14) | (offset >> 2)
            segments.append(bytes(segment))
        position = self.record_segments
        for kind, epoch, table in ((FORWARD, fwd_epoch, tables[0]), (REVERSE, rev_epoch, tables[1])):
            for index in range(len(table) // SLOTS_PER_SEG):
                slots = table[index * SLOTS_PER_SEG:(index + 1) * SLOTS_PER_SEG]
                header = segment_header(kind, index, epoch, (first_sequence + position) & 0xFFFFFFFF)
                segments.append(used_part(header + struct.pack(f"<{SLOTS_PER_SEG}I", *slots)))
                position += 1
        return segments
