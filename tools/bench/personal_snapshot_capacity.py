"""Measure personal snapshot staging with production factory images."""
import argparse
import hashlib
import json

from snapshot_feasibility import ROOT, apply, occupied, open_image, synthetic
from build_native_image import SEGMENT, build
from compact_search_index import add_search_index
from hybrid import format_stroke
from import_personal import parse_named
from native_base_update import PINS
from native_snapshot import CompactImage, compile_snapshot, inspect_free, personal_baseline, plan_change
from native_update import NoSpace, effective_entries


def measure(image, source, compact, *, fragmented=False, verify=True):
    original = open_image(image)
    protected = occupied(original)
    if fragmented:
        protected.update(i for i in range(2, 255, 8) if i not in protected)
    free, run = inspect_free(protected)
    report = dict(free=free, largest_run=run, reserve=3)
    try:
        update = plan_change(original, protected, original.personal, compact, convert=True)
    except NoSpace as exc:
        return dict(report, staged=False, reason=str(exc))
    if verify:
        new, view = apply(image, original, update)
        current = effective_entries(view, "user_dictionary", lambda at, n: new[at:at + n])
        expected = {"/".join(map(format_stroke, key)): text.decode()
                    for key, text in personal_baseline(original, source).items()}
        assert current == expected and view.personal == original.personal
    return dict(report, staged=True, staging_segments=len(update.segments), verified=len(current) if verify else None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--search-index", action="store_true", help="Measure search-indexed snapshot images")
    args = parser.parse_args()
    raw = (ROOT / "build/plover/plover-main.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == PINS["Plover Base"][2]
    corpora = {"pinned-plover": parse_named(raw)[0], "legal-200k": synthetic(200000)}
    empty, _ = build(ROOT / "build")
    report = {}
    for name, entries in corpora.items():
        compact = compile_snapshot("user_dictionary", entries)
        if args.search_index:
            compact = CompactImage(add_search_index(compact.data), compact.pointers)
        metrics = dict(entries=len(entries), compact_bytes=len(compact.data),
                       contiguous_required=(len(compact.data) + SEGMENT - 1)//SEGMENT)
        metrics["fill_existing_personal"] = measure(empty, entries, compact)
        metrics["fragmented"] = measure(empty, entries, compact, fragmented=True, verify=False)
        try:
            full, _ = build(ROOT / "build", personal=entries)
        except ValueError as exc:
            metrics["full_journal"] = dict(staged=False, reason=str(exc))
        else:
            metrics["full_journal"] = measure(full, None, compact)
        report[name] = metrics
    from pathlib import Path
    Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
