#!/usr/bin/env python3
"""Exercise the exact tonight fixtures through the real host planner."""
import json
from pathlib import Path
import sys
import tempfile

from test_native_stage import ROOT, fixture
from test_native_personal_snapshot import opened, publish_image
from build_native_image import SEGMENT, build
from compile_dict import parse_outline
from import_personal import parse_named
from native_import_plan import compare
from native_snapshot import compile_snapshot, personal_baseline, plan_change
from native_update import Edit, effective_entries, encode_record

sys.path.insert(0, str(ROOT / "tools/bench"))
from snapshot_feasibility import synthetic

FIXTURES = ROOT / "docs/fixtures/adaptive-snapshot"


def entries(file):
    return parse_named((FIXTURES / file).read_bytes())[0]


def occupied(view):
    return {0, 1, 255} | {i for start, count, *_ in view.extents for i in range(start, start + count)}


def main():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        fixture(path)
        initial, _ = build(path, personal=entries("seed-journal.json"))
        original = opened(initial)
        baseline = personal_baseline(original, entries("fill-personal.json"))
        assert baseline[parse_outline("STKPWHR")] == b"sqcheck preserved"
        compact = compile_snapshot("user_dictionary", baseline)
        staged = plan_change(original, occupied(original), original.personal, compact, convert=True)
        image = publish_image(initial, staged, 3)
        before = opened(image)
        offset = before.anchor * SEGMENT + 256 + before.used[before.anchor]
        edits = [(strokes, 0, text) for strokes, text in entries("edit-personal.json").items()]
        edits.append((parse_outline("PWOB"), 1, b""))
        for sequence, (strokes, op, text) in enumerate(edits, before.highwater + 1):
            edit = Edit(offset, before.personal, op, strokes, text)
            record = encode_record(edit, sequence)
            image[offset:offset + len(record)] = record
            offset += edit.span
        after = opened(image)
        expected = json.loads((FIXTURES / "expected-after.json").read_text(encoding="utf-8"))
        assert effective_entries(after, "user_dictionary", lambda at, n: image[at:at + n]) == expected
        compact = compile_snapshot("user_dictionary", entries("expected-after.json"))
        consolidate = plan_change(after, occupied(after), after.personal, compact, consolidate=True)
        restored = publish_image(image, consolidate, 5)
        current = opened(restored)
        assert effective_entries(current, "user_dictionary", lambda at, n: restored[at:at + n]) == expected
        assert current.personal == original.personal and not current.entries
        cut_source = synthetic(50000)
        layout, _ = compare(current, occupied(current), "SQCut", cut_source, 16,
                            compile_snapshot("SQCut", cut_source))
        staged_cut = layout["options"]["snapshot"]
        assert staged_cut["eligible"] and staged_cut["staging_segments"] > 8
        print(f"SQCut preview has {staged_cut['staging_segments']} staging segments in the host fixture")
        print("Tonight's fixture verified fill-only precedence, edits, tombstones, cold exports and consolidation")


if __name__ == "__main__":
    main()
