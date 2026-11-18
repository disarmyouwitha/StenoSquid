#!/usr/bin/env python3
"""Candidate-B fake NOR: catalog/overlay semantics and torn-operation recovery."""

from copy import deepcopy
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/storage"))
from hybrid import (
    FakeNor,
    Hybrid,
    PowerCut,
    NoSpace,
    Corrupt,
    SEGMENT,
    PAGE,
    compact_entries,
    XIP,
    format_stroke,
    parse_outline,
)


def semantic():
    nor = FakeNor(24)
    db = Hybrid.provision(nor)
    a = db.add("Lapwing", {"KAT": "cat", "TKOG": "dog", "HE/HROE": "hello"})
    b = db.add("Plover", {"KAT": "kitty", "TKOG": "hound", "TEFT": "test"})
    assert db.lookup("KAT") == (a, "cat")
    db.edit(a, "KAT", "ça va")
    assert db.lookup("KAT") == (a, "ça va")
    db.edit(a, "TKOG", None)
    assert db.lookup("TKOG") == (b, "hound")
    assert db.export(a) == {"KAT": "ça va", "HE/HROE": "hello"}
    assert db.reverse("ça va") == [(a, "KAT")]
    assert db.reverse("cat") == []
    db.enable(a, False)
    assert db.lookup("KAT") == (b, "kitty")
    db.enable(a, True)
    db.reorder([b, a])
    assert db.lookup("KAT") == (b, "kitty")
    db.reorder([a, b])
    db.rename(a, "Renamed")
    assert db.dictionary(a)["name"] == "Renamed"
    db.replace(a, {"KAT": "upstream changed", "TKOG": "puppy"})
    assert db.lookup("KAT") == (a, "ça va") and db.lookup("TKOG") == (b, "hound")
    db.clear_edit(a, "KAT")
    assert db.lookup("KAT") == (a, "upstream changed")
    old = db.dictionary(a)["base"]["start"]
    with db.snapshot() as snapshot:
        db.replace(a, {"KAT": "new"})
        db.edit(b, "TEFT", "edit")
        db.rename(b, "Plover again")
        assert old in db.occupied()
        assert db.lookup("KAT", snapshot) == (a, "upstream changed")
    db.relocate(a)
    assert db.lookup("KAT") == (a, "new")
    db = Hybrid(nor)
    assert db.lookup("KAT") == (a, "new") and db.lookup("TEFT") == (b, "edit")
    db.delete(a)
    assert db.lookup("KAT") == (b, "kitty")
    c = db.add("New", {})
    assert c > a and c > b
    db.edit(c, "KAT", "named import")
    db.reorder([c, b])
    assert db.lookup("KAT") == (c, "named import")
    # One shared data pool, with no per-dictionary fixed start/quota.
    assert all(e["start"] >= 2 for e in db.extents(db.state))
    try:
        db.reorder([c, c])
    except ValueError:
        pass
    else:
        raise AssertionError("Duplicate reorder accepted")
    print(
        "B semantics: priorities, overlays, tombstones, reverse/export, updates, relocation, pinning and reboot passed"
    )


def fault_case(label, action):
    nor = FakeNor(14)
    db = Hybrid.provision(nor)
    a = db.add("A", {"KAT": "cat", "TKOG": "dog"})
    b = db.add("B", {"KAT": "kitty"})
    before = deepcopy(db.state)
    dry = nor.clone()
    new = Hybrid(dry)
    action(new, a, b)
    after = deepcopy(new.state)
    operations = dry.operations
    for point in range(1, operations + 1):
        for tear in (0, 1, 127, 4095):
            trial = nor.clone()
            trial.cut_at = point
            trial.tear = tear
            try:
                action(Hybrid(trial), a, b)
            except PowerCut:
                pass
            else:
                raise AssertionError("Injection did not trip")
            recovered = Hybrid(trial)
            assert recovered.state in (before, after), (label, point, tear)
            # Recovery reads only: repeated reboot cannot mutate or erase flash.
            raw = bytes(trial.data)
            assert Hybrid(trial).state == recovered.state and bytes(trial.data) == raw
            assert recovered.lookup("KAT") is not None
    print(f"{label}: {operations * 4} torn-operation cut points passed")


def refusals():
    nor = FakeNor(7)
    db = Hybrid.provision(nor)
    a = db.add("A", {"KAT": "cat"})
    with db.snapshot():
        db.edit(a, "KAT", "first")
        before = bytes(nor.data)
        state = deepcopy(db.state)
        try:
            db.replace(a, {"KAT": "replacement"})
        except NoSpace:
            pass
        else:
            raise AssertionError("Near-full replacement accepted")
        assert (
            bytes(nor.data) == before
            and db.state == state
            and db.lookup("KAT") == (a, "first")
        )
    unknown = FakeNor(8)
    unknown.data[4 * SEGMENT] = 0
    original = bytes(unknown.data)
    for open_fn in (Hybrid, Hybrid.provision):
        try:
            open_fn(unknown)
        except Corrupt:
            pass
        else:
            raise AssertionError("Unknown nonblank image accepted")
        assert bytes(unknown.data) == original
    # A corrupt newest catalog falls back to its intact predecessor.
    db.rename(a, "changed")
    new_slot = db.slot
    old_catalog = db._catalog(1 - new_slot)[0]
    nor.data[new_slot * SEGMENT + 32] ^= 1
    assert Hybrid(nor).state == old_catalog
    print(
        "B refusal/recovery: no writes on insufficient space/unknown flash, corrupt-catalog fallback passed"
    )


def fragmentation():
    nor = FakeNor(18)
    db = Hybrid.provision(nor)
    identities = [db.add(str(i), {"KAT": str(i)}) for i in range(13)]
    for identity in identities[::2]:
        db.delete(identity)
    # Expire the catalog generation that still held the final removed extent.
    db.rename(identities[1], "live")
    db.rename(identities[1], "live again")
    raw = bytes(nor.data)
    assert 18 - len(db.occupied()) >= 8
    try:
        db._allocate(5, set())
    except NoSpace:
        pass
    else:
        raise AssertionError("Fragmented contiguous request unexpectedly fit")
    assert bytes(nor.data) == raw
    assert db.lookup("KAT") == (identities[1], "1")
    # Same-identity compact updates are rebuilt for the new actual address.
    for mask in [
        1,
        2,
        3,
        256,
        512,
        1024,
        8192,
        16384,
        (1 << 23) | 8,
        (1 << 24) | 8,
        0x1FFFFFF,
    ]:
        assert parse_outline(format_stroke(mask)) == (mask,)
    print(
        "B allocation: fragmented free pool refuses without erasing; formatter/parser round trips passed"
    )


def corruption_quarantine():
    nor = FakeNor(14)
    db = Hybrid.provision(nor)
    identity = db.add("A", {"KAT": "old"})
    db.replace(identity, {"KAT": "new"})
    damaged = db.dictionary(identity)["base"]["start"] * SEGMENT
    nor.data[damaged] ^= 1
    raw = bytes(nor.data)
    recovered = Hybrid(nor)
    assert recovered.quarantined and recovered.lookup("KAT") == (identity, "old")
    try:
        recovered.edit(identity, "KAT", "attempt")
    except Corrupt:
        pass
    else:
        raise AssertionError("Quarantined image allowed writes")
    assert bytes(nor.data) == raw
    print(
        "B corruption: referenced-image failure falls back read-only without reclaiming quarantined extents"
    )


def main():
    semantic()
    refusals()
    fragmentation()
    corruption_quarantine()
    for label, action in [
        ("add", lambda d, a, b: d.add("C", {"KAT": "new"})),
        ("edit", lambda d, a, b: d.edit(a, "KAT", "changed")),
        ("tombstone", lambda d, a, b: d.edit(a, "KAT", None)),
        ("update", lambda d, a, b: d.replace(a, {"KAT": "fresh", "TEFT": "test"})),
        ("relocate", lambda d, a, b: d.relocate(a)),
        ("reorder", lambda d, a, b: d.reorder([b, a])),
        ("delete", lambda d, a, b: d.delete(a)),
    ]:
        fault_case(label, action)


if __name__ == "__main__":
    main()
