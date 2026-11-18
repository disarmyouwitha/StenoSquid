"""Host-only shared append journal for the Candidate-B reference model.

No production format or firmware migration. Extents and catalog ownership use
Hybrid; edits append CRC-protected records and commit last without catalog
rewrites. Compaction stages live records before publishing a replacement root.
"""

from contextlib import contextmanager
from copy import deepcopy
import struct
import zlib
from hybrid import (
    Hybrid,
    FakeNor,
    Corrupt,
    NoSpace,
    SEGMENT,
    PAGE,
    XIP,
    encode,
    canonical,
    reader,
    validate_entries,
)

SEG_MAGIC = b"SQJSEG01"
REC_MAGIC = b"SQJR"
COMMIT = 0x434F4D54
SEG_HEADER = struct.Struct("<8sQIIII")
REC_HEADER = struct.Struct("<4sIQIIII")


class JournalHybrid(Hybrid):
    ALIGNMENT = PAGE

    @staticmethod
    def initial_state():
        return {"generation": 0, "next_id": 1, "dictionaries": [], "journals": []}

    @classmethod
    def provision(cls, nor):
        if any(b != 255 for b in nor.data):
            raise Corrupt("Explicit provisioning requires blank NOR")
        instance = object.__new__(cls)
        instance.nor = nor
        instance.pins = []
        instance.locked = False
        instance.slot = 1
        instance.quarantined = False
        instance.state = cls.initial_state()
        instance.retained = []
        instance._publish(deepcopy(instance.state))
        return cls(nor)

    @staticmethod
    def extents(state):
        yield from Hybrid.extents(state)
        yield from state["journals"]

    def _validate(self, state):
        if "journals" not in state:
            raise Corrupt("Not a journal prototype catalog")
        basic = {k: v for k, v in state.items() if k != "journals"}
        super()._validate(basic)
        occupied = {0, 1}
        for extent in Hybrid.extents(state):
            occupied.update(range(extent["start"], extent["start"] + extent["count"]))
        epochs = set()
        for extent in state["journals"]:
            if (
                extent["count"] != 1
                or extent["start"] < 2
                or (extent["start"] + 1) * SEGMENT > len(self.nor.data)
            ):
                raise Corrupt("Journal bounds")
            if extent["start"] in occupied or extent["epoch"] in epochs:
                raise Corrupt("Journal ownership overlap")
            occupied.add(extent["start"])
            epochs.add(extent["epoch"])
            self._scan(extent)
        previous = 0
        for entry in self.records(state):
            if entry["sequence"] <= previous:
                raise Corrupt("Journal sequence order")
            previous = entry["sequence"]

    def _scan(self, extent):
        start = extent["start"] * SEGMENT
        raw = self.nor.data
        magic, epoch, crc, a, b, c = SEG_HEADER.unpack_from(raw, start)
        if (
            magic != SEG_MAGIC
            or epoch != extent["epoch"]
            or zlib.crc32(raw[start : start + 16]) != crc
            or (a, b, c) != (0, 0, 0)
        ):
            raise Corrupt("Journal segment header")
        offset = PAGE
        entries = []
        while offset + REC_HEADER.size <= SEGMENT:
            address = start + offset
            header = bytes(raw[address : address + REC_HEADER.size])
            if header == b"\xff" * REC_HEADER.size:
                return entries, offset, False
            magic, span, sequence, size, crc, hcrc, commit = REC_HEADER.unpack(header)
            if commit != COMMIT:
                return entries, offset, True  # Torn tail: never reuse its pages.
            if (
                magic != REC_MAGIC
                or span % self.ALIGNMENT
                or span < REC_HEADER.size + 1
                or span > SEGMENT - offset
                or size < 1
                or size > span - REC_HEADER.size
                or zlib.crc32(header[:24]) != hcrc
            ):
                raise Corrupt("Committed journal header")
            payload = bytes(
                raw[address + REC_HEADER.size : address + REC_HEADER.size + size]
            )
            if zlib.crc32(struct.pack("<Q", sequence) + payload) != crc:
                raise Corrupt("Committed journal CRC")
            entry = self._decode_payload(payload)
            if (
                set(entry) != {"id", "outline", "op", "text"}
                or not isinstance(entry["id"], int)
                or entry["id"] < 1
                or entry["op"] not in ("set", "remove", "reset")
            ):
                raise Corrupt("Journal schema")
            validate_entries({entry["outline"]: entry["text"]}, allow_tombstone=True)
            if entry["op"] == "set" and entry["text"] is None:
                raise Corrupt("Set requires translation")
            if entry["op"] != "set" and entry["text"] is not None:
                raise Corrupt("Non-set text")
            entry["sequence"] = sequence
            entries.append(entry)
            offset += span
        return entries, offset, False

    def records(self, state=None):
        state = state or self.state
        for extent in state["journals"]:
            entries, _, _ = self._scan(extent)
            for entry in entries:
                if entry["sequence"] <= state.get("_reader_sequence", 2**64 - 1):
                    yield entry

    def overlay_for(self, item, state=None):
        result = {}
        for entry in self.records(state):
            if entry["id"] != item["id"]:
                continue
            key = canonical(entry["outline"])
            if entry["op"] == "reset":
                result.pop(key, None)
            else:
                result[key] = {"outline": entry["outline"], "text": entry["text"]}
        return result

    def overlay(self, item):
        return self.overlay_for(item)

    def lookup(self, outline, state=None):
        state = state or self.state
        key = canonical(outline)
        for item in state["dictionaries"]:
            if not item["enabled"]:
                continue
            overlay = self.overlay_for(item, state)
            if key in overlay:
                if overlay[key]["text"] is not None:
                    return item["id"], overlay[key]["text"]
                continue
            if item["base"]:
                value = reader(
                    self.blob(item["base"]), XIP + item["base"]["start"] * SEGMENT
                )(outline)
                if value is not None:
                    return item["id"], value
        return None

    @contextmanager
    def snapshot(self):
        state = deepcopy(self.state)
        state["_reader_sequence"] = max(
            (e["sequence"] for e in self.records()), default=0
        )
        self.pins.append(state)
        try:
            yield state
        finally:
            self.pins.remove(state)

    @staticmethod
    def _payload(entry):
        return encode(entry)

    @staticmethod
    def _decode_payload(payload):
        try:
            return __import__("json").loads(payload)
        except ValueError as error:
            raise Corrupt("Journal JSON") from error

    @classmethod
    def _encoded(cls, entry, sequence):
        payload = cls._payload(entry)
        alignment = cls.ALIGNMENT
        span = (
            (len(payload) + REC_HEADER.size + alignment - 1) // alignment
        ) * alignment
        if span > SEGMENT - PAGE:
            raise ValueError("Journal entry too large")
        first = struct.pack(
            "<4sIQII",
            REC_MAGIC,
            span,
            sequence,
            len(payload),
            zlib.crc32(struct.pack("<Q", sequence) + payload),
        )
        return first + struct.pack("<II", zlib.crc32(first), 0xFFFFFFFF), payload, span

    def _initialize(self, extent):
        self.nor.erase(extent["start"])
        first = struct.pack("<8sQ", SEG_MAGIC, extent["epoch"])
        self.nor.write(
            extent["start"] * SEGMENT,
            first + struct.pack("<IIII", zlib.crc32(first), 0, 0, 0),
        )

    def _write(self, extent, offset, entry, sequence):
        header, payload, span = self._encoded(entry, sequence)
        address = extent["start"] * SEGMENT + offset
        self.nor.write(address, header)
        self.nor.write(address + REC_HEADER.size, payload)
        self.nor.program(address + 28, struct.pack("<I", COMMIT))
        return span

    def _append(self, identity, outline, text, op):
        if self.quarantined:
            raise Corrupt("Quarantined catalog")
        if self.locked:
            raise RuntimeError("Concurrent writer")
        self.dictionary(identity)
        validate_entries({outline: text}, allow_tombstone=True)
        self.locked = True
        try:
            sequence = max((e["sequence"] for e in self.records()), default=0) + 1
            entry = {"id": identity, "outline": outline, "op": op, "text": text}
            _, _, span = self._encoded(entry, sequence)
            extent = self.state["journals"][-1] if self.state["journals"] else None
            if extent:
                _, offset, sealed = self._scan(extent)
            else:
                offset, sealed = PAGE, True
            if sealed or offset + span > SEGMENT:
                extent = {
                    "start": self._allocate(1, set()),
                    "count": 1,
                    "epoch": max(
                        (e["epoch"] for e in self.state["journals"]), default=0
                    )
                    + 1,
                }
                state = deepcopy(self.state)
                state["journals"].append(extent)
                if len(encode(state)) + 32 > SEGMENT:
                    raise NoSpace("Catalog full")
                self._initialize(extent)
                self._publish(state)
                offset = PAGE
            self._write(extent, offset, entry, sequence)
        finally:
            self.locked = False

    def edit(self, identity, outline, text):
        self._append(identity, outline, text, "remove" if text is None else "set")

    def clear_edit(self, identity, outline):
        self._append(identity, outline, None, "reset")

    def compact(self):
        """Stage every live overlay, publish once, retain old segments for readers."""
        if self.quarantined:
            raise Corrupt("Quarantined catalog")
        if self.locked:
            raise RuntimeError("Concurrent writer")
        self.locked = True
        try:
            sequence = max((e["sequence"] for e in self.records()), default=0) + 1
            records = []
            for item in self.state["dictionaries"]:
                for key, entry in sorted(self.overlay_for(item).items()):
                    records.append(
                        {
                            "id": item["id"],
                            "outline": entry["outline"],
                            "text": entry["text"],
                            "op": "remove" if entry["text"] is None else "set",
                        }
                    )
            plans = []
            reserved = set()
            offset = SEGMENT
            for entry in records:
                _, _, span = self._encoded(entry, sequence)
                if offset + span > SEGMENT:
                    extent = {
                        "start": self._allocate(1, reserved),
                        "count": 1,
                        "epoch": max(
                            (e["epoch"] for e in self.state["journals"]), default=0
                        )
                        + 1
                        + len(plans),
                    }
                    reserved.add(extent["start"])
                    plans.append((extent, []))
                    offset = PAGE
                plans[-1][1].append((offset, entry, sequence))
                offset += span
                sequence += 1
            state = deepcopy(self.state)
            state["journals"] = [e for e, _ in plans]
            if len(encode(state)) + 32 > SEGMENT:
                raise NoSpace("Catalog full")
            for extent, entries in plans:
                self._initialize(extent)
                for offset, entry, sequence in entries:
                    self._write(extent, offset, entry, sequence)
            self._validate(state)
            self._publish(state)
        finally:
            self.locked = False
