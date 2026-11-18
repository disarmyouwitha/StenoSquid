#!/usr/bin/env python3
"""Shared overlay append/compaction correctness and torn-operation recovery."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/storage"))
from hybrid import FakeNor, PowerCut, Corrupt, NoSpace, SEGMENT
from journal import JournalHybrid


def view(db):
    return (
        [
            (d["id"], d["name"], d["enabled"], db.export(d["id"]))
            for d in db.state["dictionaries"]
        ],
        db.lookup("KAT"),
        db.lookup("TKOG"),
    )


def fixture():
    nor = FakeNor(20)
    db = JournalHybrid.provision(nor)
    a = db.add("A", {"KAT": "cat", "TKOG": "dog"})
    b = db.add("B", {"KAT": "kitty"})
    return nor, db, a, b


def faults(label, prepare, action):
    nor, db, a, b = fixture()
    prepare(db, a, b)
    before = view(db)
    dry = nor.clone()
    next_db = JournalHybrid(dry)
    action(next_db, a, b)
    after = view(next_db)
    for point in range(1, dry.operations + 1):
        for tear in (0, 1, 127, 4095):
            trial = nor.clone()
            trial.cut_at = point
            trial.tear = tear
            try:
                action(JournalHybrid(trial), a, b)
            except PowerCut:
                pass
            else:
                raise AssertionError("Missed cut")
            trial.cut_at = None
            restored = JournalHybrid(trial)
            assert view(restored) in (before, after), (label, point, tear)
            raw = bytes(trial.data)
            assert view(JournalHybrid(trial)) == view(restored) and raw == bytes(
                trial.data
            )
            target = restored.state["dictionaries"][0]["id"]
            restored.edit(target, "TKOG", "post-recovery")
            assert JournalHybrid(trial).lookup("TKOG") == (target, "post-recovery")
    print(label, dry.operations * 4, "cut points passed")


def extra_checks():
    nor, db, a, b = fixture()
    # Seal the current segment with a torn next header, then roll over. The
    # unfinished entry must not be replayed or overwrite prior data on retry.
    db.edit(a, "KAT", "old")
    extent = db.state["journals"][-1]
    _, offset, _ = db._scan(extent)
    nor.program(extent["start"] * SEGMENT + offset, b"BAD!")
    db = JournalHybrid(nor)
    db.edit(a, "KAT", "retried")
    assert len(db.state["journals"]) == 2 and db.lookup("KAT") == (a, "retried")
    db.compact()
    db = JournalHybrid(nor)
    assert db.lookup("KAT") == (a, "retried")
    # Space refusal is preflighted before touching existing segments.
    small = FakeNor(7)
    s = JournalHybrid.provision(small)
    x = s.add("X", {"KAT": "cat"})
    s.add("Y", {"KAT": "kitty"})
    raw = bytes(small.data)
    try:
        s.edit(x, "KAT", "new")
    except NoSpace:
        pass
    else:
        raise AssertionError("Reserved space was consumed")
    assert raw == bytes(small.data) and s.lookup("KAT") == (x, "cat")
    constrained = FakeNor(8)
    limited = JournalHybrid.provision(constrained)
    left = limited.add("L", {"KAT": "cat"})
    limited.add("R", {"KAT": "kitty"})
    limited.edit(left, "KAT", "kept")
    raw = bytes(constrained.data)
    try:
        limited.compact()
    except NoSpace:
        pass
    else:
        raise AssertionError("Compaction consumed staging reserve")
    assert bytes(constrained.data) == raw and JournalHybrid(constrained).lookup(
        "KAT"
    ) == (left, "kept")
    # Deleted dictionary edits cannot bleed into a newly allocated stable ID.
    db.delete(a)
    c = db.add("C", {})
    assert c > b
    assert db.lookup("KAT") == (b, "kitty")
    db.edit(c, "KAT", "third")
    db.reorder([c, b])
    db.compact()
    assert JournalHybrid(nor).lookup("KAT") == (c, "third")


def main():
    extra_checks()
    nor, db, a, b = fixture()
    db.edit(a, "KAT", "first")
    generation = db.state["generation"]
    erases = nor.erase_sectors
    with db.snapshot() as old:
        db.edit(a, "KAT", "second")
        assert db.lookup("KAT", old) == (a, "first")
        assert db.state["generation"] == generation and nor.erase_sectors == erases
        db.edit(a, "KAT", None)
        assert db.lookup("KAT") == (b, "kitty")
        db.compact()
        assert db.lookup("KAT", old) == (a, "first")
    db.clear_edit(a, "KAT")
    assert db.lookup("KAT") == (a, "cat")
    db = JournalHybrid(nor)
    assert db.lookup("KAT") == (a, "cat")
    db.edit(a, "KAT", "override")
    db.replace(a, {"KAT": "new base"})
    assert db.lookup("KAT") == (a, "override")
    assert db.reverse("override") == [(a, "KAT")]
    db.compact()
    assert JournalHybrid(nor).lookup("KAT") == (a, "override")
    # A corrupted committed record is never mistaken for a torn uncommitted tail.
    extent = db.state["journals"][0]
    nor.data[extent["start"] * SEGMENT + 256 + 32] ^= 1
    original = bytes(nor.data)
    try:
        recovered = JournalHybrid(nor)
        assert recovered.quarantined
    except Corrupt:
        pass
    assert original == bytes(nor.data)
    prepare = lambda d, a, b: d.edit(a, "KAT", "old")
    faults("append", prepare, lambda d, a, b: d.edit(a, "KAT", "new"))
    faults("new-segment", lambda d, a, b: None, lambda d, a, b: d.edit(a, "KAT", "new"))
    faults("reset", prepare, lambda d, a, b: d.clear_edit(a, "KAT"))
    faults("tombstone", prepare, lambda d, a, b: d.edit(a, "KAT", None))
    faults("compact", prepare, lambda d, a, b: d.compact())
    faults("multi-page-append", prepare, lambda d, a, b: d.edit(a, "KAT", "x" * 2047))
    faults("reorder-with-journal", prepare, lambda d, a, b: d.reorder([b, a]))
    faults("delete-with-journal", prepare, lambda d, a, b: d.delete(a))
    print(
        "Shared journal semantics, snapshot isolation, reboot, compaction and corruption checks passed"
    )


if __name__ == "__main__":
    main()
