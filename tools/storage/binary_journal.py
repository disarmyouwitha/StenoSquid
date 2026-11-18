"""Binary-record experiment on fake NOR; deliberately not a device format."""

import struct
from hybrid import Corrupt, format_stroke, parse_outline, validate_entries
from journal import JournalHybrid

# ID, opcode, stroke count, UTF-8 byte count; followed by 32-bit strokes and text.
PREFIX = struct.Struct("<IBBH")
OPS = ("set", "remove", "reset")


class BinaryJournal(JournalHybrid):
    ALIGNMENT = 4

    @staticmethod
    def _payload(entry):
        validate_entries({entry["outline"]: entry["text"]}, allow_tombstone=True)
        strokes = parse_outline(entry["outline"])
        text = (entry["text"] or "").encode("utf-8")
        if not 0 < entry["id"] <= 0xFFFFFFFF:
            raise ValueError("Dictionary ID outside uint32")
        if len(text) > 2047:
            raise ValueError("Translation exceeds byte limit")
        if entry["op"] != "set" and entry["text"] is not None:
            raise ValueError("Non-set text")
        if entry["op"] == "set" and entry["text"] is None:
            raise ValueError("Set requires translation")
        return (
            PREFIX.pack(entry["id"], OPS.index(entry["op"]), len(strokes), len(text))
            + b"".join(stroke.to_bytes(4, "little") for stroke in strokes)
            + text
        )

    @staticmethod
    def _decode_payload(payload):
        if len(payload) < PREFIX.size:
            raise Corrupt("Binary record prefix")
        identity, op, count, length = PREFIX.unpack_from(payload)
        if (
            not identity
            or op >= len(OPS)
            or not 1 <= count <= 16
            or length > 2047
            or len(payload) != PREFIX.size + 4 * count + length
            or (op != 0 and length)
        ):
            raise Corrupt("Binary record bounds")
        strokes = [
            int.from_bytes(payload[i : i + 4], "little")
            for i in range(PREFIX.size, PREFIX.size + 4 * count, 4)
        ]
        if any(stroke == 0 or stroke >= 1 << 25 for stroke in strokes):
            raise Corrupt("Binary stroke mask")
        try:
            text = payload[PREFIX.size + 4 * count :].decode("utf-8")
            outline = "/".join(format_stroke(stroke) for stroke in strokes)
            validate_entries({outline: text}, allow_tombstone=True)
        except (ValueError, UnicodeError) as error:
            raise Corrupt("Binary record text/outline") from error
        return {
            "id": identity,
            "op": OPS[op],
            "outline": outline,
            "text": text if op == 0 else None,
        }
