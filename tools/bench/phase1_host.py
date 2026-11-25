#!/usr/bin/env python3
"""Run host-only 64 MiB qualification and dormant-library comparisons."""
import argparse
import json
import math
from pathlib import Path
import statistics
import struct

from run_typing_replay import execute
from build_native_image import SEGMENT, catalog_header, write_journal_header, seed_entries
from compile_dict import parse_outline
from native_update import EXTENT
from phase1_fixture import DEEP, tape
from snapshot_feasibility import occupied, open_image

CONTROL = {1, 6, 8, DEEP}
CASES = ("A", "B", "C", "D")


def case_image(source, view, case, hot, disabled_only, enabled_ids=CONTROL):
    payload = bytearray(view.payload[:32])
    output = bytearray(b"\xff" * len(source))
    rows, extents = [], []
    for original in view.rows:
        row = bytearray(original)
        identity, _, base = struct.unpack_from("<IIH", row)
        enabled = identity in enabled_ids
        if case == "A" and not enabled and identity > 7:
            continue
        struct.pack_into("<I", row, 4, enabled)
        if base != 65535:
            if case == "A" and not enabled:
                struct.pack_into("<HBB", row, 8, 65535, 0, 2)
            else:
                extent = view.extents[base]
                struct.pack_into("<H", row, 8, len(extents))
                extents.append(EXTENT.pack(*extent))
                begin = extent[0] * SEGMENT
                end = begin + extent[1] * SEGMENT
                output[begin:end] = source[begin:end]
        rows.append(row)
    allocated = {index for raw in extents for first, count, *_ in [EXTENT.unpack(raw)]
                 for index in range(first, first + count)}
    journal = view.journals[0][0]
    extents.append(write_journal_header(output, journal, 1))
    records = {
        (identity, key): (record.operation, record.text)
        for (identity, key), record in view.entries.items()
        if identity in enabled_ids or case == "D" or (case == "C" and key not in hot)
    }
    if case == "D" and disabled_only is not None:
        records[(32, disabled_only)] = (0, b"disabled only")
    seed_entries(output, extents, journal, max(journal, *allocated) + 1, records)
    struct.pack_into("<HHQ", payload, 4, len(rows), len(extents), len(records))
    payload = bytes(payload) + b"".join(rows) + b"".join(extents)
    for slot in (0, 1):
        at = slot * SEGMENT
        output[at:at + 32 + len(payload)] = catalog_header(slot + 1, payload) + payload
    result = open_image(output)
    return output, dict(configured_slots=32, catalog_rows=len(rows), enabled=len(enabled_ids),
                        installed=sum(row[11] != 2 for row in rows),
                        journal_records=len(records), occupied_blocks=len(occupied(result)) - 1)


def repeated(phase, minimum_seconds):
    cycles = math.ceil(minimum_seconds * 1e9 / phase["active_ns"])
    events, prefix = [], ""
    for loop in range(cycles):
        for deadline, stroke, text in phase["events"]:
            expected = f"{prefix} {text}" if prefix and text else prefix or text
            events.append((loop * phase["active_ns"] + deadline, stroke, expected))
        prefix = events[-1][2]
    return dict(phase, words=phase["words"] * cycles, chords=len(events),
                active_ns=phase["active_ns"] * cycles, duration_ns=phase["active_ns"] * cycles,
                expected_text=prefix, events=events)


def replay_full(binary, base, fixture, output, paced):
    output.mkdir()
    (output / "simulated-pool.bin").symlink_to((base / "simulated-pool.bin").resolve())
    phases = {int(rate): phase for rate, phase in fixture["phases"].items()}
    if paced:
        phases[300] = repeated(phases[300], 180)
    execute(binary, output, fixture, phases, not paced, verify_negative=not paced, live_tail=True)
    result = all(not row["deadline_overruns"] and not row["disabled_compact_provider_calls"]
                 for row in phases.values())
    return dict(result="pass" if result else "fail", phases=phases)


def replay_dormant(binary, base, fixture, output, trials):
    source = (base / "simulated-pool.bin").read_bytes()
    view = open_image(source)
    outlines = {row[0] for row in fixture["probes"] if row[2] == DEEP}
    misses = fixture["full_stack_misses"]
    # All 16 active case edits have unique text. Choose only hits whose
    # winning dictionary belongs to the four-dictionary control.
    probes = [row for row in fixture["probes"] if row[2] in CONTROL or row[2] == 0]
    hot = {parse_outline(outline) for outline in outlines | set(misses)}
    images, summary = {}, {}
    for case in CASES:
        root = output / case
        root.mkdir(parents=True, exist_ok=False)
        image, state = case_image(source, view, case, hot, parse_outline(misses[0]))
        images[case] = root / "simulated-pool.bin"
        images[case].write_bytes(image)
        summary[case] = dict(state=state, trials=[])
    # The same four winners, priority, pure misses and miss/undo schedule.
    deep_hits = {parse_outline(stroke): value.encode() for stroke, value, owner in probes if owner == DEEP}
    edits = {parse_outline(stroke): value.encode() for stroke, value, owner in probes
             if owner in CONTROL - {DEEP} and value.startswith("edited")}
    for trial in range(trials):
        order = CASES[trial % len(CASES):] + CASES[:trial % len(CASES)]
        for case in order:
            run = images[case].parent / f"trial-{trial + 1}"
            run.mkdir()
            (run / "simulated-pool.bin").symlink_to(images[case].resolve())
            phases = {300: tape(deep_hits, edits, [parse_outline(o) for o in misses])[300]}
            execute(binary, run, {"probes": probes}, phases, True, verify_negative=trial == 0, live_tail=True)
            summary[case]["trials"].append(phases[300])
    for case in CASES:
        result = summary[case]
        for percentile in ("p95", "p99"):
            value = statistics.median(t["engine_us"][percentile] for t in result["trials"])
            result[f"median_trial_{percentile}_us"] = value
            control = summary["A"][f"median_trial_{percentile}_us"]
            result[f"{percentile}_delta_us"] = value - control
            result[f"{percentile}_delta_percent"] = 100 * (value / control - 1)
        result["disabled_probes"] = sum(t["disabled_compact_provider_calls"] for t in result["trials"])
        result["threshold_met"] = result["disabled_probes"] == 0 and all(
            result[f"{p}_delta_percent"] <= 5 for p in ("p95", "p99"))
    return summary


def replay_active(binary, base, fixture, output):
    source = (base / "simulated-pool.bin").read_bytes()
    view = open_image(source)
    states = {struct.unpack_from("<I", row)[0]: bool(row[4]) for row in view.rows}
    ordered = list(states)
    preferred = sorted(ordered, key=lambda identity: (identity not in CONTROL, not states[identity]))
    misses = [parse_outline(outline) for outline in fixture["full_stack_misses"]]
    deep = [row for row in fixture["probes"] if row[2] == DEEP and row[1].startswith("probe")]
    overlap = parse_outline(deep[0][0])
    deep_hits = {parse_outline(outline): word.encode() for outline, word, _ in deep[1:]}
    result = {}
    for count in (4, 8, 16, 24, 32):
        enabled = set(preferred[:count])
        probes = [row for row in fixture["probes"][1:] if row[2] in enabled or row[2] == 0]
        for identity in ordered:
            if identity not in enabled:
                continue
            edit = view.entries.get((identity, overlap))
            if edit is not None and edit.operation == 0:
                probes.append((deep[0][0], edit.text.decode(), identity))
                break
            if identity == DEEP:
                probes.append(deep[0])
                break
        edit_hits = {parse_outline(stroke): value.encode() for stroke, value, _ in probes
                     if value and value.startswith("edited")}
        phase = {300: tape(deep_hits, edit_hits, misses)[300]}
        run = output / str(count)
        run.mkdir(parents=True)
        image, state = case_image(source, view, "D", set(), None, enabled)
        (run / "simulated-pool.bin").write_bytes(image)
        execute(binary, run, {"probes": probes}, phase, True, verify_negative=False, live_tail=True)
        result[count] = dict(state=state, metrics=phase[300])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--paced", action="store_true", help="Run full 300 WPM for at least three minutes")
    parser.add_argument("--dormant-trials", type=int, default=0)
    parser.add_argument("--active-sweep", action="store_true")
    args = parser.parse_args()
    fixture = json.loads((args.fixture / "fixture.json").read_bytes())
    args.out.mkdir(parents=True, exist_ok=False)
    result = dict(scope="Host engine, simulated NOR and captured key output", phase="full")
    try:
        result["full"] = replay_full(args.binary, args.fixture, fixture, args.out / "full", args.paced)
        if args.dormant_trials:
            result["phase"] = "dormant"
            result["dormant"] = replay_dormant(args.binary, args.fixture, fixture,
                                               args.out / "dormant", args.dormant_trials)
        if args.active_sweep:
            result["phase"] = "active"
            result["active"] = replay_active(args.binary, args.fixture, fixture, args.out / "active")
    except Exception as error:
        result.update(result="fail", error=str(error))
        raise
    finally:
        (args.out / "report.json").write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
