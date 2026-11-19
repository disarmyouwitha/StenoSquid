"""Host-only Candidate-B correctness prototype. Never accepts a hardware device.

Two committed catalogs and copy-on-write compact/overlay extents share one fake
NOR pool. Overlay snapshots deliberately trade write efficiency for a small,
fault-testable reference model; production journaling/indexing is not selected.
"""

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import struct
import sys
import zlib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/dict_compiler"))
from compile_dict import DictionaryCollectionBuilder, parse_outline
from jsc4 import reader

SEGMENT = 65536
PAGE = 256
XIP = 0x12000000
MAGIC = b"SQBHOST1"
COMMIT = 0x434F4D54
HEADER = struct.Struct("<8sQIIII")


class PowerCut(RuntimeError):
    pass


class NoSpace(RuntimeError):
    pass


class Corrupt(ValueError):
    pass


def encode(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical(outline):
    return "/".join(str(n) for n in parse_outline(outline))


class FakeNor:
    """Page programs and sector erases, with torn-operation fault injection."""

    def __init__(self, segments=255, data=None):
        self.data = bytearray(b"\xff" * (segments * SEGMENT) if data is None else data)
        self.operations = 0
        self.program_bytes = 0
        self.erase_sectors = 0
        self.cut_at = None
        self.tear = 0

    def clone(self):
        return FakeNor(data=self.data)

    def _count(self):
        self.operations += 1
        return self.operations == self.cut_at

    def program(self, address, data):
        if (
            address < 0
            or address + len(data) > len(self.data)
            or not data
            or address // PAGE != (address + len(data) - 1) // PAGE
        ):
            raise ValueError("Invalid page program")
        previous = self.data[address : address + len(data)]
        if int.from_bytes(data, "little") & ~int.from_bytes(previous, "little"):
            raise ValueError("NOR program tried to restore cleared bits")
        cut = self._count()
        n = min(self.tear, len(data)) if cut else len(data)
        self.data[address : address + n] = data[:n]
        self.program_bytes += n
        if cut:
            raise PowerCut("Torn page program")

    def erase(self, segment):
        if segment < 0 or (segment + 1) * SEGMENT > len(self.data):
            raise ValueError("Invalid erase")
        # Model a block erase as observable 4 KiB sector steps.
        for address in range(segment * SEGMENT, (segment + 1) * SEGMENT, 4096):
            cut = self._count()
            n = min(self.tear, 4096) if cut else 4096
            self.data[address : address + n] = b"\xff" * n
            self.erase_sectors += 1
            if cut:
                raise PowerCut("Torn erase")

    def write(self, address, data):
        while data:
            n = min(len(data), PAGE - address % PAGE)
            self.program(address, data[:n])
            address += n
            data = data[n:]


class Hybrid:
    def __init__(self, nor):
        self.nor = nor
        self.pins = []
        self.locked = False
        valid = []
        present = []
        for slot in range(2):
            catalog = self._catalog(slot)
            if catalog:
                present.append(catalog[0])
                try:
                    self._validate(catalog[0])
                except (
                    Corrupt,
                    ValueError,
                    KeyError,
                    TypeError,
                    IndexError,
                    AssertionError,
                    struct.error,
                ):
                    continue
                valid.append((catalog[0]["generation"], slot, catalog[0]))
        if not valid:
            raise Corrupt(
                "No valid committed catalog; explicit blank provisioning required"
            )
        _, self.slot, self.state = max(valid, key=lambda item: item[0])
        # Preserve every structurally committed generation, even quarantined ones.
        self.retained = present
        self.quarantined = len(valid) != len(present)

    @classmethod
    def provision(cls, nor):
        if any(b != 255 for b in nor.data):
            raise Corrupt("Provisioning refuses nonblank flash")
        instance = object.__new__(cls)
        instance.nor = nor
        instance.pins = []
        instance.locked = False
        instance.slot = 1
        instance.quarantined = False
        instance.state = {"generation": 0, "next_id": 1, "dictionaries": []}
        instance.retained = []
        instance._publish(deepcopy(instance.state))
        return cls(nor)

    def _catalog(self, slot):
        raw = self.nor.data[slot * SEGMENT : (slot + 1) * SEGMENT]
        try:
            magic, generation, length, crc, hcrc, commit = HEADER.unpack_from(raw)
        except struct.error:
            return None
        if (
            magic != MAGIC
            or commit != COMMIT
            or not 0 < length <= SEGMENT - HEADER.size
            or zlib.crc32(raw[:24]) != hcrc
        ):
            return None
        payload = bytes(raw[HEADER.size : HEADER.size + length])
        if zlib.crc32(payload) != crc:
            return None
        try:
            state = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            return None
        if (
            not isinstance(state, dict)
            or generation < 1
            or state.get("generation") != generation
        ):
            return None
        return state, slot

    @staticmethod
    def extents(state):
        for item in state["dictionaries"]:
            for name in ("base", "overlay"):
                if item.get(name):
                    yield item[name]

    def blob(self, extent):
        start = extent["start"] * SEGMENT
        raw = bytes(self.nor.data[start : start + extent["bytes"]])
        if hashlib.sha256(raw).hexdigest() != extent["sha256"]:
            raise Corrupt("Extent hash mismatch")
        return raw

    def _validate(self, state):
        if not isinstance(state.get("next_id"), int) or state["next_id"] < 1:
            raise Corrupt("Invalid next identity")
        if set(state) != {"generation", "next_id", "dictionaries"} or not isinstance(
            state["dictionaries"], list
        ):
            raise Corrupt("Catalog schema")
        used = {0, 1}
        ids = set()
        for item in state["dictionaries"]:
            if not isinstance(item["id"], int) or item["id"] < 1 or item["id"] in ids:
                raise Corrupt("Duplicate/invalid dictionary identity")
            if not isinstance(item["name"], str) or not isinstance(
                item["enabled"], bool
            ):
                raise Corrupt("Dictionary metadata")
            ids.add(item["id"])
            for kind in ("base", "overlay"):
                extent = item.get(kind)
                if not extent:
                    continue
                start, count, size = extent["start"], extent["count"], extent["bytes"]
                if (
                    start < 2
                    or count < 1
                    or size < 1
                    or size > count * SEGMENT
                    or (start + count) * SEGMENT > len(self.nor.data)
                ):
                    raise Corrupt("Extent bounds")
                blocks = set(range(start, start + count))
                if blocks & used:
                    raise Corrupt("Overlapping extent")
                used |= blocks
                raw = self.blob(extent)
                if kind == "base":
                    lookup = reader(raw, XIP + start * SEGMENT)
                    for outline, text in compact_entries(raw, XIP + start * SEGMENT):
                        if lookup(outline) != text:
                            raise Corrupt("Compact enumeration parity")
                else:
                    data = json.loads(raw)
                    if not isinstance(data, dict):
                        raise Corrupt("Invalid overlay")
                    for key, entry in data.items():
                        if canonical(entry["outline"]) != key or (
                            entry["text"] is not None
                            and not isinstance(entry["text"], str)
                        ):
                            raise Corrupt("Overlay identity/type")
        if state["next_id"] <= max(ids, default=0):
            raise Corrupt("ID reuse")

    def occupied(self):
        result = {0, 1}
        for state in [self.state, *self.retained, *self.pins]:
            for extent in self.extents(state):
                result.update(range(extent["start"], extent["start"] + extent["count"]))
        return result

    def _allocate(self, count, reserved):
        used = self.occupied() | reserved
        # Three segments remain free after staging; fragmentation can still refuse.
        if len(self.nor.data) // SEGMENT - len(used) - count < 3:
            raise NoSpace("Not enough staging/GC reserve")
        for start in range(2, len(self.nor.data) // SEGMENT - count + 1):
            if not set(range(start, start + count)) & used:
                return start
        raise NoSpace("No contiguous extent; old data retained")

    def _publish(self, state):
        state["generation"] = self.state["generation"] + 1
        payload = encode(state)
        if len(payload) > SEGMENT - HEADER.size:
            raise NoSpace("Catalog full")
        slot = 1 - self.slot
        address = slot * SEGMENT
        self.nor.erase(slot)
        head = struct.pack(
            "<8sQII", MAGIC, state["generation"], len(payload), zlib.crc32(payload)
        )
        header = head + struct.pack("<II", zlib.crc32(head), 0xFFFFFFFF)
        self.nor.write(address, header)
        self.nor.write(address + HEADER.size, payload)
        if (
            bytes(
                self.nor.data[
                    address + HEADER.size : address + HEADER.size + len(payload)
                ]
            )
            != payload
        ):
            raise Corrupt("Catalog readback")
        self.nor.program(address + 28, struct.pack("<I", COMMIT))
        previous = deepcopy(self.state)
        self.state = state
        self.slot = slot
        self.retained = [previous] if previous["generation"] else []

    def transaction(self, change, blobs=()):
        if self.quarantined:
            raise Corrupt(
                "Corrupt committed extent quarantined; writes require explicit recovery"
            )
        if self.locked:
            raise RuntimeError("Transaction already active")
        self.locked = True
        try:
            state = deepcopy(self.state)
            reserved = set()
            plans = []
            change(state)
            # Plan all extents and catalog bytes before the first erase.
            for kind, value, target_id in blobs:
                if kind == "base":
                    validate_entries(value)
                    dummy = DictionaryCollectionBuilder(base_addr=XIP).build(
                        [("base", value)]
                    )
                    count = (len(dummy) + SEGMENT - 1) // SEGMENT
                    start = self._allocate(count, reserved)
                    raw = DictionaryCollectionBuilder(
                        base_addr=XIP + start * SEGMENT
                    ).build([("base", value)])
                    lookup = reader(raw, XIP + start * SEGMENT)
                    for outline, text in value.items():
                        if lookup(outline) != text:
                            raise Corrupt("Compiled content mismatch")
                else:
                    raw = encode(value)
                    count = (len(raw) + SEGMENT - 1) // SEGMENT
                    start = self._allocate(count, reserved)
                if len(raw) > count * SEGMENT:
                    raise Corrupt("Relocation changed extent size")
                extent = {
                    "start": start,
                    "count": count,
                    "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
                reserved.update(range(start, start + count))
                plans.append((extent, raw))
                item = next(
                    item for item in state["dictionaries"] if item["id"] == target_id
                )
                item[kind] = extent
                if kind == "base":
                    item["source_sha256"] = hashlib.sha256(encode(value)).hexdigest()
            if len(encode(state)) + HEADER.size > SEGMENT:
                raise NoSpace("Catalog full")
            for extent, raw in plans:
                for segment in range(
                    extent["start"], extent["start"] + extent["count"]
                ):
                    self.nor.erase(segment)
                self.nor.write(extent["start"] * SEGMENT, raw)
                self.blob(extent)
            self._validate(state)
            self._publish(state)
        finally:
            self.locked = False

    def add(self, name, entries, enabled=True):
        # Stable IDs allocated only in the committed catalog; names may duplicate.
        identity = self.state["next_id"]
        item = {
            "id": identity,
            "name": name,
            "enabled": enabled,
            "base": None,
            "overlay": None,
            "source_sha256": None,
        }

        def change(state):
            state["dictionaries"].append(item)
            state["next_id"] += 1

        self.transaction(change, [("base", entries, identity)] if entries else [])
        return identity

    def dictionary(self, identity, state=None):
        return next(
            item
            for item in (state or self.state)["dictionaries"]
            if item["id"] == identity
        )

    def overlay(self, item):
        return json.loads(self.blob(item["overlay"])) if item["overlay"] else {}

    def edit(self, identity, outline, text):
        key = canonical(outline)
        item = self.dictionary(identity)
        validate_entries({outline: text}, allow_tombstone=True)
        overlay = self.overlay(item)
        overlay[key] = {"outline": outline, "text": text}
        self.transaction(lambda state: None, [("overlay", overlay, identity)])

    def clear_edit(self, identity, outline):
        item = self.dictionary(identity)
        overlay = self.overlay(item)
        overlay.pop(canonical(outline), None)
        self.transaction(lambda state: None, [("overlay", overlay, identity)])

    def replace(self, identity, entries):
        self.dictionary(identity)
        if entries:
            self.transaction(lambda state: None, [("base", entries, identity)])
        else:
            self.transaction(
                lambda state: self.dictionary(identity, state).update(
                    base=None, source_sha256=None
                )
            )

    def rename(self, identity, name):
        self.transaction(
            lambda state: self.dictionary(identity, state).update(name=name)
        )

    def enable(self, identity, enabled):
        self.transaction(
            lambda state: self.dictionary(identity, state).update(enabled=bool(enabled))
        )

    def reorder(self, identities):
        if len(identities) != len(set(identities)) or set(identities) != {
            d["id"] for d in self.state["dictionaries"]
        }:
            raise ValueError("Reorder requires every stable ID once")
        self.transaction(
            lambda state: state.update(
                dictionaries=[self.dictionary(i, state) for i in identities]
            )
        )

    def delete(self, identity):
        self.dictionary(identity)
        self.transaction(
            lambda state: state.update(
                dictionaries=[d for d in state["dictionaries"] if d["id"] != identity]
            )
        )

    def lookup(self, outline, state=None):
        state = state or self.state
        key = canonical(outline)
        for item in state["dictionaries"]:
            if not item["enabled"]:
                continue
            overlay = self.overlay(item)
            if key in overlay:
                text = overlay[key]["text"]
                if text is not None:
                    return item["id"], text
                continue
            if item["base"]:
                text = reader(
                    self.blob(item["base"]), XIP + item["base"]["start"] * SEGMENT
                )(outline)
                if text is not None:
                    return item["id"], text
        return None

    @contextmanager
    def snapshot(self):
        state = deepcopy(self.state)
        self.pins.append(state)
        try:
            yield state
        finally:
            self.pins.remove(state)

    def export(self, identity):
        item = self.dictionary(identity)
        result = {}
        if item["base"]:
            for outline, text in compact_entries(
                self.blob(item["base"]), XIP + item["base"]["start"] * SEGMENT
            ):
                result[canonical(outline)] = (outline, text)
        for key, entry in self.overlay(item).items():
            if entry["text"] is None:
                result.pop(key, None)
            else:
                result[key] = (entry["outline"], entry["text"])
        return {outline: text for outline, text in result.values()}

    def reverse(self, text):
        results = []
        for item in self.state["dictionaries"]:
            if not item["enabled"]:
                continue
            for outline, value in self.export(item["id"]).items():
                if value == text and self.lookup(outline) == (item["id"], text):
                    results.append((item["id"], outline))
        return results

    def relocate(self, identity):
        item = self.dictionary(identity)
        if not item["base"]:
            return
        entries = dict(
            compact_entries(
                self.blob(item["base"]), XIP + item["base"]["start"] * SEGMENT
            )
        )
        self.replace(identity, entries)


def validate_entries(entries, allow_tombstone=False):
    seen = {}
    for outline, text in entries.items():
        strokes = parse_outline(outline)
        if not 1 <= len(strokes) <= 16:
            raise ValueError("Outline length")
        if text is None and allow_tombstone:
            continue
        if (
            not isinstance(text, str)
            or "\0" in text
            or len(text.encode("utf-8")) > 2047
        ):
            raise ValueError("Translation bounds")
        if strokes in seen and seen[strokes] != text:
            raise ValueError("Conflicting canonical outline")
        seen[strokes] = text


def format_stroke(mask):
    if not mask or mask >> 25:
        raise Corrupt("Unsupported compact stroke")
    left = "".join(c for bit, c in enumerate("STKPWHR", 1) if mask & (1 << bit))
    middle = "".join(c for bit, c in enumerate("AO*EU", 8) if mask & (1 << bit))
    right = "".join(c for bit, c in enumerate("FRPBLGTSDZ", 13) if mask & (1 << bit))
    return (
        ("#" if mask & 1 else "")
        + ("^" if mask & (1 << 23) else "")
        + ("+" if mask & (1 << 24) else "")
        + left
        + middle
        + ("-" if right and not middle else "")
        + right
    )


def compact_entries(blob, base, member=0, count=1):
    """Enumerate one member’s real JSC4 data blocks using bitmap counts."""
    reader(blob, base, member, count)

    def word(offset):
        return struct.unpack_from("<I", blob, offset)[0]

    def pointer(offset):
        value = word(offset) - base
        if not 0 <= value < len(blob):
            raise Corrupt("Compact pointer bounds")
        return value

    definition = pointer(36 + 4 * member)
    text = pointer(definition + 8)
    table = pointer(definition + 12)
    maximum = blob[definition + 1]
    if not 1 <= maximum <= 16 or table + 12 * maximum > len(blob):
        raise Corrupt("Compact stroke table bounds")
    for length in range(1, maximum + 1):
        entry = table + 12 * (length - 1)
        mask = word(entry)
        if not mask:
            continue
        data = pointer(entry + 4)
        blocks = pointer(entry + 8)
        block_count = (mask + 1) // 128
        if (
            (mask + 1) % 128
            or (mask + 1) & mask
            or blocks + 20 * block_count > len(blob)
        ):
            raise Corrupt("Compact bitmap bounds")
        count = sum(
            word(blocks + 20 * b + 4 * i).bit_count()
            for b in range((mask + 1) // 128)
            for i in range(4)
        )
        stride = 3 + 3 * length
        if data + count * stride > len(blob):
            raise Corrupt("Compact entries outside image")
        for index in range(count):
            position = data + index * stride
            text_offset = int.from_bytes(blob[position : position + 3], "little")
            start = text + text_offset
            if not text <= start < len(blob):
                raise Corrupt("Compact text outside image")
            end = blob.index(0, start)
            outlines = [
                format_stroke(
                    int.from_bytes(
                        blob[position + 3 + 3 * i : position + 6 + 3 * i], "little"
                    )
                )
                for i in range(length)
            ]
            yield "/".join(outlines), blob[start:end].decode("utf-8")
