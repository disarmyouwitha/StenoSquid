#!/usr/bin/env python3
"""Fake-NOR publication, complete index validation, snapshots and reverse pages."""

import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "storage"))
from hybrid import FakeNor, PowerCut, Corrupt, NoSpace, SEGMENT, canonical
from binary_journal import BinaryJournal
from indexed_journal import IndexedJournal, TAIL_LIMIT


def fixture(segments=32):
    nor = FakeNor(segments)
    db = IndexedJournal.provision(nor)
    a = db.add("A", {"KAT": "cat", "TKOG": "dog", "HE/HROE": "hello"})
    b = db.add("B", {"KAT": "kitty", "TKOG": "hound"})
    return nor, db, a, b


def view(db):
    return (
        [db.lookup(k) for k in ("KAT", "TKOG", "HE/HROE", "TEFT")],
        sorted((i, canonical(o)) for i, o in db.reverse("cat")),
        [db.export(d["id"]) for d in db.state["dictionaries"]],
    )


def fault_check(label, prepare, action):
    nor, db, a, b = fixture()
    prepare(db, a, b)
    before = view(db)
    dry = nor.clone()
    staged = IndexedJournal(dry)
    action(staged, a, b)
    after = view(staged)
    count = 0
    for point in range(1, dry.operations + 1):
        for tear in (0, 1, 127, 4095):
            trial = nor.clone()
            trial.cut_at, trial.tear = point, tear
            try:
                action(IndexedJournal(trial), a, b)
            except PowerCut:
                pass
            else:
                raise AssertionError("Missed cut")
            trial.cut_at = None
            original = bytes(trial.data)
            restored = IndexedJournal(trial)
            assert view(restored) in (before, after), (label, point, tear)
            assert bytes(trial.data) == original  # Opening/reverse never repair.
            restored.edit(a, "KAT", "after recovery")
            assert IndexedJournal(trial).lookup("KAT") == (a, "after recovery")
            count += 1
    print(f"{label}: {count} torn-operation cases passed", flush=True)
    return count


def semantics():
    nor, db, a, b = fixture()
    db.edit(a, "KAT", "cat")
    db.edit(a, "TKOG", None)
    db.checkpoint()
    assert db.lookup("TKOG") == (b, "hound")
    assert db.reverse("cat") == [(a, "KAT")]
    with db.snapshot() as old:
        db.clear_edit(a, "KAT")
        db.edit(a, "TKOG", "ça va")
        db.checkpoint()
        db.compact()
        assert db.lookup("KAT", old) == (a, "cat")
        assert db.lookup("TKOG", old) == (b, "hound")
    assert db.lookup("TKOG") == (a, "ça va")
    db.enable(a, False)
    assert db.lookup("KAT") == (b, "kitty")
    db.enable(a, True)
    db.replace(a, {"KAT": "new cat"})
    assert db.lookup("KAT") == (a, "new cat")
    db.rename(a, "renamed")
    db.reorder([b, a])
    assert db.reverse("new cat") == []
    db.delete(b)
    db.compact()
    assert db.reverse("new cat") == [(a, "KAT")]
    assert view(IndexedJournal(nor)) == view(db)
    db.clear_edit(a, "TKOG")
    db.compact()
    assert db.state["journals"] == [] and db.tail == []
    assert db.lookup("KAT") == (a, "new cat")

    # Compare indexed queries with the independent replay backend on identical
    # fake-NOR bytes, stripping only index metadata from a separate catalog view.
    nor, db, a, b = fixture()
    words = ("KAT", "TKOG", "TEFT", "HE/HROE")
    import random

    rng = random.Random(43)
    for i in range(320):
        identity, outline = rng.choice((a, b)), rng.choice(words)
        op = rng.randrange(3)
        if op == 0:
            db.clear_edit(identity, outline)
        else:
            db.edit(identity, outline, None if op == 1 else f"text {i % 5}")
        if i % 41 == 0:
            db.checkpoint()
        if i % 97 == 0:
            db.compact()
        replay = object.__new__(BinaryJournal)
        replay.nor = nor
        replay.state = {k: v for k, v in db.state.items() if k != "checkpoint"}
        for word in words:
            assert db.lookup(word) == replay.lookup(word)
        for text in ("cat", "text 1", "dog", "not found"):
            assert sorted(db.reverse(text)) == sorted(replay.reverse(text))
    assert view(IndexedJournal(nor)) == view(db)


def reverse_pages():
    nor, db, a, b = fixture()
    # More than sixteen distinct outlines legitimately share one translation.
    from hybrid import format_stroke

    for mask in range(1, 66):
        db.edit(a, format_stroke(mask), "shared")
    db.checkpoint()
    cursor = db.reverse_pages("shared", budget=7)
    first = next(cursor)
    assert db.pins and len(first) <= 7
    # Cursor keeps its original view across edits, disabled dictionaries,
    # replacement indexes and record compaction. Current view must differ.
    db.edit(a, format_stroke(1), "changed")
    db.enable(a, False)
    db.compact()
    pages = [first, *cursor]
    result = [item for page in pages for item in page]
    assert len(result) == len(set(result)) == 65
    assert all(len(page) <= 7 for page in pages) and not db.pins
    assert db.reverse("shared") == []
    db.enable(a, True)
    assert len(db.reverse("shared")) == 64
    cursor = db.reverse_pages("shared", budget=1)
    next(cursor)
    assert db.pins
    cursor.close()
    assert not db.pins
    for invalid in (0, 257):
        try:
            list(db.reverse_pages("shared", invalid))
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid reverse budget")


def bounds_and_corruption():
    nor, db, a, b = fixture()
    for i in range(TAIL_LIMIT):
        db.edit(a, "KAT", f"v{i}")
    assert len(db.tail) == TAIL_LIMIT
    original = bytes(nor.data)
    try:
        db.edit(a, "BAD!", "invalid")
    except ValueError:
        pass
    else:
        raise AssertionError("Invalid request")
    assert bytes(nor.data) == original
    db.edit(a, "KAT", "rollover")
    assert len(db.tail) == 1 and db.lookup("KAT") == (a, "rollover")
    assert IndexedJournal(nor).lookup("KAT") == (a, "rollover")

    # Reserve refusal occurs before erasing/programming anything.
    nor, db, a, b = fixture(9)
    db.edit(a, "KAT", "kept")
    original = bytes(nor.data)
    try:
        db.checkpoint()
    except NoSpace:
        pass
    else:
        raise AssertionError("Checkpoint consumed reserve")
    assert original == bytes(nor.data) and db.lookup("KAT") == (a, "kept")

    nor, db, a, b = fixture()
    db.edit(a, "KAT", "cat")
    db.edit(a, "TKOG", "dog")
    db.checkpoint()
    # Change the index while also updating its hash in a staged in-memory root:
    # semantic validation must reject omissions/duplicates, not just bad CRCs.
    import hashlib
    from copy import deepcopy

    state = deepcopy(db.state)
    extent = state["checkpoint"]["forward"]
    address = extent["start"] * SEGMENT
    original = bytes(nor.data[address : address + extent["bytes"]])
    nor.data[address : address + 4] = original[4:8]
    extent["sha256"] = hashlib.sha256(
        nor.data[address : address + extent["bytes"]]
    ).hexdigest()
    try:
        db._validate(state)
    except Corrupt:
        pass
    else:
        raise AssertionError("Duplicate index entry accepted")
    # On-disk committed corruption quarantines/falls back without writing.
    original_flash = bytes(nor.data)
    recovered = IndexedJournal(nor)
    assert recovered.quarantined
    assert recovered.lookup("KAT") == (a, "cat")
    assert bytes(nor.data) == original_flash


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    cases = {}
    semantics()
    reverse_pages()
    bounds_and_corruption()
    prepare = lambda d, a, b: (d.edit(a, "KAT", "cat"), d.edit(a, "TKOG", None))
    cases["checkpoint publication"] = fault_check(
        "checkpoint publication", prepare, lambda d, a, b: d.checkpoint()
    )
    cases["compact records and indexes"] = fault_check(
        "compact records and indexes", prepare, lambda d, a, b: d.compact()
    )
    cases["checkpoint replacement"] = fault_check(
        "checkpoint replacement",
        lambda d, a, b: (prepare(d, a, b), d.checkpoint(), d.edit(a, "KAT", "changed")),
        lambda d, a, b: d.checkpoint(),
    )

    def full_tail(d, a, b):
        for i in range(TAIL_LIMIT):
            d.edit(a, "KAT", f"tail {i}")

    cases["automatic checkpoint before append"] = fault_check(
        "automatic checkpoint before append",
        full_tail,
        lambda d, a, b: d.edit(a, "KAT", "rolled"),
    )
    print(
        "Indexed publication, recovery, tail, pinned reverse and independent oracle checks passed"
    )
    if args.report:
        root = Path(__file__).resolve().parents[2]
        sources = [
            "tools/storage/hybrid.py",
            "tools/storage/journal.py",
            "tools/storage/binary_journal.py",
            "tools/storage/indexed_journal.py",
            "tools/tests/test_indexed_journal.py",
        ]
        report = dict(
            schema=1,
            host_only=True,
            passed=True,
            cases=cases,
            total_torn_cases=sum(cases.values()),
            mixed_edits=320,
            mixed_seed=43,
            shared_translation_outlines=65,
            source_sha256={
                p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in sources
            },
            limits=[
                "Small fake-NOR fixtures, not hardware or capacity evidence",
                "Whole-live-overlay compaction and unrestricted host builder",
                "Native reader tests and full firmware resource tests are separate",
            ],
        )
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
