"""Optional SQS1 search tables for one user-owned JSC4 member.

Text index tokens sort by translation offset and then outline length and keys.
Partial-outline buckets map a stroke to all longer outlines containing it.
Every token holds an image-relative packed JSC4 row offset and stroke length.
"""
from collections import defaultdict
import struct

FOOTER = struct.Struct("<4s4I")
MAGIC = b"SQS1"


def add_search_index(blob, base=0):
    def word(offset):
        return struct.unpack_from("<I", blob, offset)[0]

    def u24(offset):
        if offset < 0 or offset + 3 > len(blob):
            raise ValueError("Invalid compact entry offset")
        return int.from_bytes(blob[offset:offset + 3], "little")

    definition = word(36) - base
    if blob[:8] != b"JSC4\x01\x00\x00\x00" or not 0 <= definition < len(blob) - 15:
        raise ValueError("Expected a single compact dictionary")
    maximum = blob[definition + 1]
    table = word(definition + 12) - base
    if not 1 <= maximum <= 16 or not 0 <= table <= len(blob) - maximum * 12:
        raise ValueError("Invalid compact search source")
    reverse = []
    partial = defaultdict(list)
    for length in range(1, maximum + 1):
        mask, data, bitmap = struct.unpack_from("<III", blob, table + 12 * (length - 1))
        if not mask:
            continue
        data -= base
        bitmap -= base
        blocks = (mask + 1) // 128
        if (mask + 1) % 128 or mask & (mask + 1) or not 0 <= data <= bitmap <= len(blob) - blocks * 20:
            raise ValueError("Invalid compact bitmap bounds")
        count = sum(word(bitmap + 20 * b + 4 * i).bit_count() for b in range(blocks) for i in range(4))
        size = 3 * (length + 1)
        if data + count * size > bitmap:
            raise ValueError("Compact rows overlap their bitmap")
        for offset in range(data, data + count * size, size):
            token = (offset << 4) | (length - 1)
            strokes = tuple(u24(offset + 3 * (index + 1)) for index in range(length))
            reverse.append((u24(offset), length, strokes, token))
            if length > 1:
                for stroke in set(strokes):
                    partial[stroke].append((length, strokes, token))
    if len(blob) >= 1 << 28 or len(blob) % 4:
        raise ValueError("Compact image cannot represent aligned search offsets")
    result = bytearray(blob)
    for _, _, _, token in sorted(reverse):
        result.extend(struct.pack("<I", token))
    postings = 0
    for stroke, outlines in sorted(partial.items()):
        result.extend(struct.pack("<II", stroke, postings))
        postings += len(outlines)
    for _, outlines in sorted(partial.items()):
        for _, _, token in sorted(outlines):
            result.extend(struct.pack("<I", token))
    result.extend(FOOTER.pack(MAGIC, len(blob), len(reverse), len(partial), postings))
    return bytes(result)


def validate_search_index(blob, base=0):
    if len(blob) < FOOTER.size or blob[-FOOTER.size:-FOOTER.size + 4] != MAGIC:
        return False
    _, legacy, _, _, _ = FOOTER.unpack_from(blob, len(blob) - FOOTER.size)
    if not 40 <= legacy < len(blob) - FOOTER.size or add_search_index(blob[:legacy], base) != blob:
        raise ValueError("Compact search index failed verification")
    return True
