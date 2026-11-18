"""Host-only transactional index publication on fake NOR. Never opens devices.

The oracle/builder uses Python heaps and replay on open/write. Forward queries
use a 256-address tail and packed indexes; this is not a firmware implementation.
"""

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import struct
import zlib

from binary_journal import BinaryJournal
from hybrid import (
    Corrupt,
    NoSpace,
    SEGMENT,
    PAGE,
    XIP,
    encode,
    parse_outline,
    reader,
    compact_entries,
)
from journal import REC_HEADER, REC_MAGIC, COMMIT

ADDRESS = struct.Struct("<I")
TAIL_LIMIT = 256


def key(entry):
    return entry["id"], parse_outline(entry["outline"])


class IndexedJournal(BinaryJournal):
    @staticmethod
    def initial_state():
        return {**BinaryJournal.initial_state(), "checkpoint": None}

    def __init__(self, nor):
        super().__init__(nor)
        self._rebuild_tail()

    @staticmethod
    def extents(state):
        yield from BinaryJournal.extents(state)
        checkpoint = state.get("checkpoint")
        if checkpoint:
            for name in ("forward", "reverse"):
                if checkpoint[name]:
                    yield checkpoint[name]

    def _locations(self, state):
        for extent in state["journals"]:
            entries, _, _ = self._scan(extent)
            offset = PAGE
            for entry in entries:
                if entry["sequence"] <= state.get("_reader_sequence", 2**64 - 1):
                    yield extent["start"] * SEGMENT + offset, entry
                offset += self._encoded(
                    {k: v for k, v in entry.items() if k != "sequence"},
                    entry["sequence"],
                )[2]

    def _record(self, address, state):
        segment, offset = divmod(address, SEGMENT)
        if (
            offset < PAGE
            or address % 4
            or not any(e["start"] == segment for e in state["journals"])
        ):
            raise Corrupt("Index record ownership")
        raw = self.nor.data
        magic, span, sequence, size, crc, hcrc, commit = REC_HEADER.unpack_from(
            raw, address
        )
        if (
            magic != REC_MAGIC
            or commit != COMMIT
            or span % 4
            or span < 36
            or span > SEGMENT - offset
            or not 1 <= size <= span - REC_HEADER.size
            or zlib.crc32(raw[address : address + 24]) != hcrc
        ):
            raise Corrupt("Indexed record header")
        payload = bytes(
            raw[address + REC_HEADER.size : address + REC_HEADER.size + size]
        )
        if zlib.crc32(struct.pack("<Q", sequence) + payload) != crc:
            raise Corrupt("Indexed record checksum")
        entry = self._decode_payload(payload)
        entry["sequence"] = sequence
        if sequence > state.get("_reader_sequence", 2**64 - 1):
            raise Corrupt("Record beyond reader snapshot")
        return entry

    def _validate(self, state):
        if "checkpoint" not in state:
            raise Corrupt("Not an indexed prototype catalog")
        super()._validate({k: v for k, v in state.items() if k != "checkpoint"})
        checkpoint = state["checkpoint"]
        if checkpoint is None:
            return
        if (
            set(checkpoint) != {"through", "forward", "reverse"}
            or not isinstance(checkpoint["through"], int)
            or checkpoint["through"] < 0
        ):
            raise Corrupt("Checkpoint schema")
        occupied = {0, 1}
        for extent in BinaryJournal.extents(state):
            occupied.update(range(extent["start"], extent["start"] + extent["count"]))
        live = {}
        maximum = 0
        for address, entry in self._locations(state):
            maximum = max(maximum, entry["sequence"])
            if entry["sequence"] <= checkpoint["through"]:
                live[key(entry)] = (address, entry)
        if checkpoint["through"] > maximum:
            raise Corrupt("Checkpoint beyond committed journal")
        active = [(k, value) for k, value in live.items() if value[1]["op"] != "reset"]
        expected = {
            "forward": [value[0] for k, value in sorted(active)],
            "reverse": [
                value[0]
                for k, value in sorted(
                    active,
                    key=lambda x: (
                        x[1][1]["text"].encode("utf-8")
                        if x[1][1]["text"] is not None
                        else b"",
                        x[0],
                    ),
                )
                if value[1]["op"] == "set"
            ],
        }
        for name, addresses in expected.items():
            extent = checkpoint[name]
            if not addresses:
                if extent is not None:
                    raise Corrupt("Unexpected empty index extent")
                continue
            if not extent or extent["bytes"] != 4 * len(addresses):
                raise Corrupt("Index cardinality")
            start, count = extent["start"], extent["count"]
            if (
                start < 2
                or count < 1
                or extent["bytes"] > count * SEGMENT
                or (start + count) * SEGMENT > len(self.nor.data)
            ):
                raise Corrupt("Index extent bounds")
            blocks = set(range(start, start + count))
            if blocks & occupied:
                raise Corrupt("Index ownership overlap")
            occupied |= blocks
            blob = self.blob(extent)
            if list(x[0] for x in struct.iter_unpack("<I", blob)) != addresses:
                raise Corrupt("Index sorting, coverage or record identity")

    def _rebuild_tail(self):
        through = (self.state["checkpoint"] or {}).get("through", 0)
        self.tail = []
        self.highwater = through
        for address, entry in self._locations(self.state):
            self.highwater = max(self.highwater, entry["sequence"])
            if entry["sequence"] > through:
                self.tail.append(address)
        if len(self.tail) > TAIL_LIMIT:
            raise Corrupt(
                "Recovered catalog exceeds bounded tail; explicit recovery required"
            )

    def _append(self, identity, outline, text, op):
        # Do not checkpoint/erase for an invalid request.
        self.dictionary(identity)
        self._payload(dict(id=identity, outline=outline, text=text, op=op))
        if len(self.tail) == TAIL_LIMIT:
            self.checkpoint()
        super()._append(identity, outline, text, op)
        self._rebuild_tail()

    def checkpoint(self, compact=False):
        if self.quarantined:
            raise Corrupt("Quarantined catalog")
        if self.locked:
            raise RuntimeError("Concurrent writer")
        self.locked = True
        try:
            state = deepcopy(self.state)
            live = {}
            for address, entry in self._locations(state):
                live[key(entry)] = (address, entry)
            plans, reserved = [], set()
            through = self.highwater
            if compact:
                current_ids = {d["id"] for d in state["dictionaries"]}
                latest = [
                    entry
                    for _, entry in live.values()
                    if entry["op"] != "reset" and entry["id"] in current_ids
                ]
                journals, live = [], {}
                offset = SEGMENT
                for entry in sorted(latest, key=key):
                    through += 1
                    _, _, span = self._encoded(entry, through)
                    if offset + span > SEGMENT:
                        extent = {
                            "start": self._allocate(1, reserved),
                            "count": 1,
                            "epoch": max(
                                (e["epoch"] for e in state["journals"]), default=0
                            )
                            + 1
                            + len(journals),
                        }
                        reserved.add(extent["start"])
                        journals.append(extent)
                        plans.append(("journal", extent, []))
                        offset = PAGE
                    staged = {**entry, "sequence": through}
                    plans[-1][2].append((offset, staged))
                    live[key(staged)] = (extent["start"] * SEGMENT + offset, staged)
                    offset += span
                state["journals"] = journals
                if not journals:
                    through = 0
            active = [
                (k, value) for k, value in live.items() if value[1]["op"] != "reset"
            ]
            arrays = {
                "forward": [value[0] for k, value in sorted(active)],
                "reverse": [
                    value[0]
                    for k, value in sorted(
                        active,
                        key=lambda x: (
                            x[1][1]["text"].encode("utf-8")
                            if x[1][1]["text"] is not None
                            else b"",
                            x[0],
                        ),
                    )
                    if value[1]["op"] == "set"
                ],
            }
            state["checkpoint"] = {"through": through, "forward": None, "reverse": None}
            for name, addresses in arrays.items():
                if not addresses:
                    continue
                raw = b"".join(ADDRESS.pack(address) for address in addresses)
                count = (len(raw) + SEGMENT - 1) // SEGMENT
                start = self._allocate(count, reserved)
                reserved.update(range(start, start + count))
                extent = {
                    "start": start,
                    "count": count,
                    "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
                state["checkpoint"][name] = extent
                plans.append(("index", extent, raw))
            if len(encode(state)) + 32 > SEGMENT:
                raise NoSpace("Catalog full")
            # All records, both indexes, catalog size and reserve are planned first.
            for kind, extent, payload in plans:
                if kind == "journal":
                    self._initialize(extent)
                    for offset, entry in payload:
                        self._write(extent, offset, entry, entry["sequence"])
                else:
                    for segment in range(
                        extent["start"], extent["start"] + extent["count"]
                    ):
                        self.nor.erase(segment)
                    self.nor.write(extent["start"] * SEGMENT, payload)
            self._validate(state)
            self._publish(state)
            self._rebuild_tail()
        finally:
            self.locked = False

    def compact(self):
        self.checkpoint(compact=True)

    @contextmanager
    def snapshot(self):
        state = deepcopy(self.state)
        state["_reader_sequence"] = self.highwater
        state["_tail"] = tuple(self.tail)
        self.pins.append(state)
        try:
            yield state
        finally:
            self.pins.remove(state)

    def _address_at(self, extent, position):
        if not extent or not 0 <= position < extent["bytes"] // 4:
            raise Corrupt("Index position")
        return ADDRESS.unpack_from(
            self.nor.data, extent["start"] * SEGMENT + position * 4
        )[0]

    def effective(self, identity, outline, state=None):
        state = state or self.state
        query = identity, parse_outline(outline)
        for address in reversed(state.get("_tail", self.tail)):
            entry = self._record(address, state)
            if key(entry) == query:
                return address, entry
        checkpoint = state["checkpoint"]
        extent = checkpoint["forward"] if checkpoint else None
        low, high = 0, extent["bytes"] // 4 if extent else 0
        while low < high:
            mid = (low + high) // 2
            address = self._address_at(extent, mid)
            entry = self._record(address, state)
            stored = key(entry)
            if stored == query:
                return address, entry
            if stored < query:
                low = mid + 1
            else:
                high = mid
        return None

    def lookup(self, outline, state=None):
        state = state or self.state
        for item in state["dictionaries"]:
            if not item["enabled"]:
                continue
            found = self.effective(item["id"], outline, state)
            if found and found[1]["op"] != "reset":
                if found[1]["op"] == "set":
                    return item["id"], found[1]["text"]
                continue
            if item["base"]:
                value = reader(
                    self.blob(item["base"]), XIP + item["base"]["start"] * SEGMENT
                )(outline)
                if value is not None:
                    return item["id"], value
        return None

    def reverse_pages(self, text, budget=8):
        """Pinned work pages, including empty pages; at most budget candidates/page.

        Host base enumeration is a correctness oracle, not native reverse timing.
        A consumer must close/finish the generator to release its reader pin.
        """
        if not 1 <= budget <= 256:
            raise ValueError("Reverse work budget")
        with self.snapshot() as state:
            active = {d["id"] for d in state["dictionaries"] if d["enabled"]}
            checkpoint = state["checkpoint"]
            extent = checkpoint["reverse"] if checkpoint else None
            low, high = 0, extent["bytes"] // 4 if extent else 0
            query = text.encode("utf-8")
            while low < high:
                mid = (low + high) // 2
                entry = self._record(self._address_at(extent, mid), state)
                if entry["text"].encode("utf-8") < query:
                    low = mid + 1
                else:
                    high = mid

            def candidates():
                position = low
                while extent and position < extent["bytes"] // 4:
                    address = self._address_at(extent, position)
                    entry = self._record(address, state)
                    if entry["text"] != text:
                        break
                    yield address, entry, False
                    position += 1
                for address in state["_tail"]:
                    yield address, self._record(address, state), False
                for item in state["dictionaries"]:
                    if item["id"] not in active or not item["base"]:
                        continue
                    for outline, value in compact_entries(
                        self.blob(item["base"]), XIP + item["base"]["start"] * SEGMENT
                    ):
                        yield (
                            None,
                            dict(id=item["id"], outline=outline, text=value, op="set"),
                            True,
                        )

            page, work = [], 0
            for address, entry, base in candidates():
                if (
                    entry["id"] in active
                    and entry["op"] == "set"
                    and entry["text"] == text
                ):
                    effective = self.effective(entry["id"], entry["outline"], state)
                    current = (
                        (not effective or effective[1]["op"] == "reset")
                        if base
                        else (effective and effective[0] == address)
                    )
                    if current and self.lookup(entry["outline"], state) == (
                        entry["id"],
                        text,
                    ):
                        page.append((entry["id"], entry["outline"]))
                work += 1
                if work == budget:
                    yield page
                    page, work = [], 0
            if work:
                yield page

    def reverse(self, text):
        return [item for page in self.reverse_pages(text) for item in page]
