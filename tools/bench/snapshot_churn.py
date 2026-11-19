#!/usr/bin/env python3
"""Compare both representations after real Native journal edits and cold opens."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import zlib

from snapshot_feasibility import ROOT, apply, occupied, open_image, synthetic

sys.path.insert(0, str(ROOT / "tools/tests"))
from hostlib import compiler
from build_native_image import SEGMENT, build
from hybrid import format_stroke
from native_snapshot import compile_snapshot, plan_change, plan_named_snapshot
from native_update import NoSpace, Snapshot, effective_entries, plan


def read_image(data):
    roots = [(struct.unpack_from("<QII", data, at + 8), at + 32) for at in (0, SEGMENT)]
    (generation, length, crc), root = max(roots)
    snapshot = Snapshot(lambda at, n: data[at:at + n], root, length, crc, strict=True)
    return generation, snapshot, root


def protected_of(data):
    used = set()
    for at in (0, SEGMENT):
        _, length, crc = struct.unpack_from("<QII", data, at + 8)
        reader = lambda address, n: data[address:address + n]
        used |= occupied(Snapshot(reader, at + 32, length, crc, metadata_only=True))
    return used


def make_tape(initial, limit, *, incremental=False):
    originals = list(initial)
    sequence = []
    for i in range(limit):
        key = (400 + i // 1024, i % 1024 + 1)
        operation = 0
        if not incremental and i % 5:
            if i % 5 in (1, 4):
                key = originals[(i * 13) % 64]
            else:
                key = originals[(i * 31 + i // 16) % len(originals)]
            operation = int(i % 5 == 3)
        text = "" if operation else f"trial {i:05d} " + "é" * (i % 21)
        sequence.append((operation, key, text))
    return sequence


def outline(key):
    return "/".join(map(format_stroke, key))


def collect(image, scenario, mode, total, oracle, physical, *, name):
    generation, view, _ = read_image(image)
    current = effective_entries(view, name, lambda at, n: image[at:at + n])
    assert current == oracle, (scenario, mode, total)
    live = {start: 0 for start in view.used}
    for record in view.entries.values():
        live[record.address // SEGMENT] += record.span
    stale_bytes = sum(view.used[start] - live[start] for start in view.used)
    dead = sorted(start for start, used in view.used.items() if used and not live[start])
    protected = protected_of(image)
    assert len(protected) - 1 == physical["protected"]
    assert len(occupied(view)) - 1 == physical["active"]
    planned = {}
    try:
        update = plan(view, protected)
        planned["explicit_journal_reclaim"] = update.reclaimed if update else 0
        planned["explicit_journal_staging_peak"] = len(protected | set(update.segments)) - 1 if update else None
    except NoSpace as exc:
        planned["explicit_journal_refused"] = str(exc)
    if mode == "S":
        from compile_dict import parse_outline
        compiled = compile_snapshot(name, {parse_outline(key): text.encode() for key, text in current.items()},
                                    search=False)
        planned["consolidated_bytes"] = len(compiled.data)
        try:
            next_snapshot = plan_change(view, protected, 8, compiled, consolidate=True)
            planned["consolidation_staging_segments"] = len(next_snapshot.segments)
            planned["consolidation_staging_peak"] = len(protected | set(next_snapshot.segments)) - 1
        except NoSpace as exc:
            planned["consolidation_refused"] = str(exc)
    return dict(scenario=scenario, mode=mode, edits=total, generation=generation, verified=len(current),
                active_journals=len(view.used), stale_journal_bytes=stale_bytes,
                fully_stale_segments=len(dead), reserve=3, free_after_reserve=255 - physical["protected"] - 3,
                index_bytes=4 * (physical["forward_records"] + physical["reverse_records"]),
                index_segments=sum(extent[1] for extent in view.extents if extent[4] in (3, 4)),
                **physical, **planned)


def run(binary, directory, image, name, scenario, mode, baseline, tape, identity):
    prefix = directory / f"{scenario}-{mode}"
    initial = prefix.with_suffix(".bin")
    initial.write_bytes(image)
    operations = prefix.with_suffix(".tape")
    operations.write_text("".join(f"{op} {key[0]} {key[1]} {text}\n" for op, key, text in tape), encoding="utf-8")
    process = subprocess.run([str(binary), str(initial), str(operations), str(prefix), str(identity)],
                             check=True, text=True, capture_output=True)
    keys = ("edits", "active", "protected", "largest_free_run", "forward_records", "reverse_records",
            "checkpoint_or_rollover_jobs", "peak_published_protected", "programmed_bytes", "erased_bytes", "read_bytes")
    oracle = {outline(key): text.decode() for key, text in baseline.items()}
    previous = 0
    rows = []
    for line in process.stdout.splitlines():
        physical = dict(zip(keys, map(int, line.split()), strict=True))
        current = physical.pop("edits")
        for op, key, text in tape[previous:current]:
            if op:
                oracle.pop(outline(key), None)
            else:
                oracle[outline(key)] = text
        data = Path(f"{prefix}-{current}.bin").read_bytes()
        rows.append(collect(data, scenario, mode, current, oracle, physical, name=name))
        previous = current
    return rows, hashlib.sha256(operations.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--baseline", type=int, default=10000)
    args = parser.parse_args()
    assert args.baseline >= 64
    with tempfile.TemporaryDirectory() as folder:
        directory = Path(folder)
        binary = directory / "churn"
        compiler_args = compiler()
        subprocess.run([*compiler_args, "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-DSTENO_SQUID_TEST_USER_SNAPSHOTS", "-I", str(ROOT / "tools/tests"),
                        "-I", str(ROOT / "tools/storage"), str(ROOT / "tools/bench/snapshot_churn.cc"), "-o", str(binary)],
                       check=True)
        comp_version = subprocess.run([compiler_args[0], "--version"], check=True, capture_output=True, text=True).stdout.splitlines()[0]
        image, manifest = build(ROOT / "build")
        hashes = {entry["name"]: entry["source_sha256"] for image in manifest["base_images"] for entry in image["dictionaries"]}
        base = open_image(image)
        name = "Churn"
        initial = synthetic(args.baseline)
        changes = make_tape(initial, 10000)
        measurement = []
        planners = {
            "J": plan(base, occupied(base), initial, name=name),
            "S": plan_named_snapshot(base, occupied(base), name, compile_snapshot(name, initial, search=False)),
        }
        for mode, update in planners.items():
            prepared, _ = apply(image, base, update)
            rows, tape_hash = run(binary, directory, prepared, name, "named-churn", mode, initial, changes, 8)
            measurement.extend(row for row in rows if row["edits"] != 500)
        journal_initial = synthetic(64)
        incremental, _ = build(ROOT / "build", personal=journal_initial)
        rows, incremental_hash = run(binary, directory, incremental, "user_dictionary", "active-journal-import", "J",
                                     journal_initial, make_tape(journal_initial, 1000, incremental=True), 1)
        measurement.extend(rows)

        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(dict(compiler=comp_version, source_sha256=hashes,
                                               baseline_entries=args.baseline, edit_sha256=tape_hash,
                                               incremental_sha256=incremental_hash, measurements=measurement), indent=2) + "\n")
        with args.output.with_suffix(".csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, list(dict.fromkeys(k for row in measurement for k in row)), lineterminator="\n")
            writer.writeheader()
            writer.writerows(measurement)
        print(f"Cold-open and export parity passed for {len(measurement)} recorded scenarios")


if __name__ == "__main__":
    main()
