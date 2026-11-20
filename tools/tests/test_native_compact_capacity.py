#!/usr/bin/env python3
"""Gate Native's default compact footprint against the original JSC4 with SHI1."""
import hashlib
import json
from pathlib import Path
import struct
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/bench"))
from snapshot_feasibility import apply, occupied, open_image, synthetic

from build_native_image import SEGMENT, XIP, build
from compile_dict import DictionaryCollectionBuilder
from hybrid import format_stroke
from import_personal import parse_named
from native_base_update import PINS
from native_snapshot import compile_snapshot, plan_change
from native_update import effective_entries
from verify_mapped_dict import verify


def measure(image, original, entries, expected_bytes, expected_segments):
    name = "user_dictionary"
    source = {"/".join(map(format_stroke, strokes)): text.decode() for strokes, text in entries.items()}
    reference = DictionaryCollectionBuilder(0, max_translation_bytes=2047).build([(name, source)])
    compiled = compile_snapshot(name, entries)
    assert len(reference) == expected_bytes, (len(reference), expected_bytes)
    assert compiled.data == reference, "Native's default image differs from original JSC4 + SHI1"
    segments = (len(reference) + SEGMENT - 1) // SEGMENT
    assert segments == expected_segments
    assert reference[-12:-8] == b"SHI1", "The one-stroke hint directory must remain"

    protected = occupied(original)
    update = plan_change(original, protected, original.personal, compiled, convert=True)
    output, view = apply(image, original, update)
    start, count, size, *_ = view.extents[-1]  # No overlay changes in these corpus fixtures.
    installed = output[start * SEGMENT:start * SEGMENT + size]
    assert (size, count) == (expected_bytes, expected_segments)
    assert verify(source, installed, XIP + start * SEGMENT) == len(entries)
    assert effective_entries(view, name, lambda at, n: output[at:at + n]) == source

    # Include the retained source root in the consolidation staging budget.
    consolidate = plan_change(view, protected | occupied(view), view.personal, compiled, consolidate=True)
    def allocation(kind):
        extents = [item for item in view.extents if item[4] == kind]
        return dict(bytes=sum(item[2] for item in extents), segments=sum(item[1] for item in extents))

    return dict(entries=len(entries), original_bytes=len(reference), lean_bytes=len(compiled.data),
                image_segments=segments, added_snapshot_metadata_bytes=0,
                shared_journal=allocation(2), native_forward_index=allocation(3),
                native_reverse_index=allocation(4), catalog_payload_bytes=len(update.payload),
                catalog_root_segments=2, metadata_staging_segments=len(update.segments) - segments,
                conversion_staging_segments=len(update.segments),
                consolidation_staging_segments=len(consolidate.segments), maintenance_reserve_segments=3,
                verified_forward_lookups=len(source), verified_exports=len(source))


def main():
    raw = (ROOT / "build/plover/plover-main.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PINS["Plover Base"][2]
    image, _ = build(ROOT / "build")
    original = open_image(image)
    report = {}
    for name, entries, size, segments in (
        ("pinned-plover", parse_named(raw)[0], 2339624, 36),
        ("legal-200k", synthetic(200000), 1931932, 30),
    ):
        report[name] = measure(image, original, entries, size, segments)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
