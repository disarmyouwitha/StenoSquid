#!/usr/bin/env python3
"""Exercise real pinned JSC4s in the host B allocator; never opens real flash."""

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

from hybrid import FakeNor, Hybrid, SEGMENT, canonical

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/dictionaries"))
from manifest import BASES


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--sources",
        type=Path,
        default=ROOT / "build/dictionary-phase0/footprint/sources",
    )
    p.add_argument("--output", type=Path, default=ROOT / "build/hybrid/pinned.json")
    args = p.parse_args()
    start = time.monotonic()
    nor = FakeNor()
    db = Hybrid.provision(nor)
    ids = []
    inputs = []
    sources = []
    peak = 2
    for base in BASES.values():
        for url, file, name, sha in base["sources"]:
            raw = (args.sources / file).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == sha
            entries = json.loads(raw.decode("utf-8"))
            identity = db.add(name, entries)
            ids.append(identity)
            inputs.append(entries)
            sources.append(dict(file=file, sha256=sha, url=url))
            # Export enumerates actual compact bytes at their allocated address.
            actual = {canonical(k): v for k, v in db.export(identity).items()}
            expected = {canonical(k): v for k, v in entries.items()}
            assert actual == expected
            peak = max(peak, len(db.occupied()))
            print(
                f"Installed/rebased/export-verified {name}: {len(actual):,} entries",
                flush=True,
            )
    # Pinned Lapwing overlays survive upstream replacement, and lower priority
    # wins when the overlay removes its own base entry.
    a, b = ids[:2]
    db.edit(a, "KAT", "local cat")
    assert db.lookup("KAT") == (a, "local cat")
    db.edit(a, "KAT", None)
    assert db.lookup("KAT") == (b, inputs[1]["KAT"])
    db.edit(a, "KAT", "local cat")
    with db.snapshot() as previous:
        old = db.dictionary(a)["base"]["start"]
        db.replace(a, inputs[0])
        assert db.dictionary(a)["base"]["start"] != old and old in db.occupied()
        assert db.lookup("KAT", previous) == (a, "local cat")
        assert db.lookup("KAT") == (a, "local cat")
        peak = max(peak, len(db.occupied()))
    db.clear_edit(a, "KAT")
    assert db.lookup("KAT") == (a, inputs[0]["KAT"])
    restarted = Hybrid(nor)
    assert restarted.state == db.state
    active = sum(e["count"] for e in db.extents(db.state)) + 2
    report = dict(
        schema=1,
        model_sha256=hashlib.sha256(
            Path(__file__).with_name("hybrid.py").read_bytes()
        ).hexdigest(),
        passed=True,
        sources=sources,
        entries=sum(len(x) for x in inputs),
        physical_bytes=len(nor.data),
        active_segments=active,
        retained_and_active_segments=len(db.occupied()),
        maximum_observed_occupied_segments=peak,
        active_bytes=active * SEGMENT,
        available_bytes_excluding_retained_and_three_reserve=(
            255 - len(db.occupied()) - 3
        )
        * SEGMENT,
        programmed_bytes=nor.program_bytes,
        erase_sector_steps=nor.erase_sectors,
        elapsed_host_seconds=time.monotonic() - start,
        checks=[
            "All six pinned images compiled at actual allocated XIP addresses",
            "All dictionary exports compared byte-for-byte after canonicalizing outline spelling",
            "Per-dictionary override and tombstone priority",
            "Full Lapwing replacement with a pinned old reader and preserved overlay",
            "Catalog and extents reopened after replacement",
        ],
        limits=[
            "Host-only heap-rich reference; no firmware RAM/latency claim",
            "No fragmentation defragmenter or live-segment GC",
            "Overlay snapshots are intentionally not a production write strategy",
            "Fault injection uses small fixtures separately, not every operation of these full images",
        ],
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
