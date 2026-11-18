#!/usr/bin/env python3
"""Deterministic host-only B churn experiment. Does not open USB or real flash."""

import argparse
import hashlib
import json
from pathlib import Path
import random
import time
from hybrid import FakeNor, Hybrid, SEGMENT


def exercise(operations=10000, segments=255, journal=False):
    rng = random.Random(20261117)
    nor = FakeNor(segments)
    if journal:
        from journal import JournalHybrid

        backend = JournalHybrid
    else:
        backend = Hybrid
    db = backend.provision(nor)
    base = {"KAT": "cat", "TKOG": "dog", "HE/HROE": "hello"}
    a = db.add("Base A", base)
    b = db.add("Base B", {"KAT": "kitty", "TKOG": "hound"})
    overlays = {a: {}, b: {}}
    order = [a, b]
    start = time.monotonic()
    max_occupied = len(db.occupied())
    words = [
        "KAT",
        "TKOG",
        "HE/HROE",
        "TEFT",
        "PHA",
        "TOP",
        "TPAPB",
        "SUPB",
        "RED",
        "PEPB",
    ]
    for i in range(operations):
        identity = rng.choice([a, b])
        outline = rng.choice(words)
        action = rng.randrange(4)
        if action == 0:
            db.clear_edit(identity, outline)
            overlays[identity].pop(outline, None)
        else:
            text = None if action == 1 else f"edit {i} ç"
            db.edit(identity, outline, text)
            overlays[identity][outline] = text
        if i % 37 == 0:
            order = list(reversed(order))
            db.reorder(order)
        if journal and (i + 1) % 256 == 0:
            db.compact()
        if i % 101 == 0:
            db = backend(nor)
        max_occupied = max(max_occupied, len(db.occupied()))
        for word in words:
            expected = None
            for target in order:
                original = base if target == a else {"KAT": "kitty", "TKOG": "hound"}
                if word in overlays[target]:
                    text = overlays[target][word]
                else:
                    text = original.get(word)
                if text is not None:
                    expected = (target, text)
                    break
            assert db.lookup(word) == expected
    restarted = backend(nor)
    assert restarted.state == db.state
    return dict(
        schema=1,
        model_sha256=hashlib.sha256(
            Path(__file__).with_name("hybrid.py").read_bytes()
        ).hexdigest(),
        journal=journal,
        journal_sha256=hashlib.sha256(
            Path(__file__).with_name("journal.py").read_bytes()
        ).hexdigest()
        if journal
        else None,
        operations=operations,
        seed=20261117,
        physical_bytes=segments * SEGMENT,
        final_generation=db.state["generation"],
        maximum_occupied_segments=max_occupied,
        retained_catalog_generations=2,
        final_occupied_segments=len(db.occupied()),
        programmed_bytes=nor.program_bytes,
        sector_erase_steps=nor.erase_sectors,
        elapsed_host_seconds=time.monotonic() - start,
        passed=True,
        limits=[
            "Tiny dictionaries; not full pinned corpus or capacity proof",
            "Journal compacts every 256 input edits"
            if journal
            else "Copy-on-write overlay snapshots rewrite the whole small overlay per edit",
            "Catalog still erases one slot on metadata change; no production wear claim",
            "Unreferenced extents are erased lazily on reuse; no live-segment GC or automatic defragmenter",
        ],
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operations", type=int, default=10000)
    parser.add_argument("--segments", type=int, default=255)
    parser.add_argument("--output", type=Path, default=Path("build/hybrid/churn.json"))
    parser.add_argument("--journal", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.operations <= 100000:
        parser.error("operations must be 1..100000")
    if not 10 <= args.segments <= 255:
        parser.error("segments must be 10..255")
    report = exercise(args.operations, args.segments, args.journal)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
