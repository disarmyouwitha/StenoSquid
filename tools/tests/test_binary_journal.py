#!/usr/bin/env python3
"""Run the journal's recovery/semantic cases against binary records as well."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "storage"))
from binary_journal import BinaryJournal, PREFIX
from hybrid import Corrupt, PowerCut, canonical, SEGMENT
import test_hybrid_journal as reference


def main():
    for outline in (
        "KAT",
        "HE/HROE",
        "#^+STKPWHRAO*EUFRPBLGTSDZ",
        "/".join(["KAT"] * 16),
    ):
        for op, text in (
            ("set", "ça va"),
            ("set", ""),
            ("remove", None),
            ("reset", None),
        ):
            entry = dict(id=0xFFFFFFFF, outline=outline, op=op, text=text)
            payload = BinaryJournal._payload(entry)
            decoded = BinaryJournal._decode_payload(payload)
            assert canonical(decoded.pop("outline")) == canonical(entry.pop("outline"))
            assert decoded == entry
    malformed = [
        b"",
        PREFIX.pack(0, 0, 1, 0) + b"\1\0\0\0",
        PREFIX.pack(1, 3, 1, 0) + b"\1\0\0\0",
        PREFIX.pack(1, 0, 1, 1) + b"\1\0\0\0\xff",
        PREFIX.pack(1, 0, 1, 0) + b"\0\0\0\x80",
    ]
    for payload in malformed:
        try:
            BinaryJournal._decode_payload(payload)
        except Corrupt:
            pass
        else:
            raise AssertionError("Accepted malformed binary payload")
    reference.JournalHybrid = BinaryJournal
    reference.main()
    # Every possible 4-byte-aligned header position within a 256-byte page,
    # including split headers/payloads and a commit in another program page.
    base, _, a, _ = reference.fixture()
    cut_points = 0
    offsets = set()
    for length in range(0, 256, 4):
        nor = base.clone()
        db = BinaryJournal(nor)
        db.edit(a, "KAT", "x" * length)
        extent = db.state["journals"][-1]
        _, offset, _ = db._scan(extent)
        offsets.add(offset % 256)
        assert offset == 256 + 44 + length  # Packed, without page padding.
        trial = nor.clone()
        BinaryJournal(trial).edit(a, "KAT", "new")
        for point in range(1, trial.operations + 1):
            for tear in (0, 1, 3, 127):
                damaged = nor.clone()
                damaged.cut_at, damaged.tear = point, tear
                try:
                    BinaryJournal(damaged).edit(a, "KAT", "new")
                except PowerCut:
                    pass
                else:
                    raise AssertionError("Missed packed-record cut")
                damaged.cut_at = None
                restored = BinaryJournal(damaged)
                assert restored.lookup("KAT") in ((a, "x" * length), (a, "new"))
                restored.edit(a, "KAT", "after")
                assert BinaryJournal(damaged).lookup("KAT") == (a, "after")
                cut_points += 1
        assert extent["start"] * SEGMENT + offset < len(nor.data)
    assert len(offsets) == 64
    print(
        f"Packed headers at all 64 page offsets: {cut_points} torn-write cases passed"
    )
    print("Binary payload roundtrip, malformed input and journal fault checks passed")


if __name__ == "__main__":
    main()
