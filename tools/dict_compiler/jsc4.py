"""Read JSC4 compact-map images using the pinned engine lookup algorithm."""
import struct
import zlib
from compile_dict import parse_outline


def reader(blob, base=0, index=0, count=1):
    def word(offset):
        return struct.unpack_from("<I", blob, offset)[0]

    def pointer(offset):
        value = word(offset) - base
        assert 0 <= value < len(blob), f"Pointer outside image at {offset:#x}"
        return value

    assert len(blob) % 4 == 0
    assert blob[:4] == b"JSC4" and word(4) == count and index < count
    assert word(32) == word(pointer(12) + word(8)), "Timestamp mismatch"
    definition = pointer(36 + 4 * index)
    maximum = blob[definition + 1]
    assert 0 < maximum <= 16
    text = pointer(definition + 8)
    table = pointer(definition + 12)

    def lookup(outline):
        strokes = parse_outline(outline)
        length = len(strokes)
        if not 0 < length <= maximum:
            return None
        entry = table + 12 * (length - 1)
        mask = word(entry)
        if not mask:
            return None
        data, blocks = pointer(entry + 4), pointer(entry + 8)
        index = zlib.crc32(b"".join(struct.pack("<I", stroke) for stroke in strokes)) & mask
        for _ in range(mask + 1):
            block, bit = blocks + 20 * (index // 128), index % 128
            part, shift = bit // 32, bit % 32
            bits = word(block + 4 * part)
            if not bits & (1 << shift):
                return None
            rank = (((bits << (31 - shift)) & 0xFFFFFFFF).bit_count() + word(block + 16)
                    + sum(word(block + 4 * j).bit_count() for j in range(part))) & 0xFFFFFFFF
            offset = data + rank * (3 + 3 * length)
            assert data <= offset and offset + 3 + 3 * length <= blocks
            found = tuple(int.from_bytes(blob[offset + 3 + 3 * j:offset + 6 + 3 * j], "little")
                          for j in range(length))
            if found == strokes:
                start = text + int.from_bytes(blob[offset:offset + 3], "little")
                return blob[start:blob.index(0, start)].decode()
            index = (index + 1) & mask
        return None

    return lookup
