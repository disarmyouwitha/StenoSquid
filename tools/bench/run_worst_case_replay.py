#!/usr/bin/env python3
"""Build and replay a near-full 16-dictionary Native pool with a live edit tail."""
import argparse
import hashlib
import json
from pathlib import Path
import random
import re
import struct
import subprocess

from run_typing_replay import ROOT, build_engine, compiler, execute
from build_native_image import SEGMENT, SOURCES, build
from compile_dict import parse_outline
from hybrid import format_stroke
from native_recovery import capture
from native_snapshot import compile_snapshot, plan_named_snapshot
from native_update import Edit, Update, encode_record, metadata_payload
from snapshot_feasibility import apply, occupied, open_image

RATES = (300, 350, 360, 400)
DEEPEST = "Replay Dictionary 08"
SIZE = 16_000
BULK_RECORDS = 4_000
RESERVE = 5


def letters(index):
    return "".join(chr(ord("a") + index // divisor % 26) for divisor in (676, 26, 1))


def fixture(out):
    rng = random.Random(16300400)
    present = set()
    for _, _, _, path in SOURCES[1:]:
        source = json.loads((ROOT / "build" / path).read_bytes())
        present.update(map(parse_outline, source))

    def fresh(length=1):
        while True:
            key = tuple(rng.randrange(1, 1 << 23) for _ in range(length))
            if key not in present:
                present.add(key)
                return key

    def render(key):
        return "/".join(map(format_stroke, key))

    factory = [(identity, name, True) for identity, name, _, _ in SOURCES]
    bulk = {(1 + i % len(factory), fresh(1 + i % 3)): (0, f"journal term {i}".encode())
            for i in range(BULK_RECORDS)}
    image, _ = build(ROOT / "build", collection=(factory, bulk, 8))
    view = open_image(image)
    deep = [(fresh(), "term" + letters(i)) for i in range(168)]
    edited = [(fresh(), "edit" + letters(i)) for i in range(16)]
    misses = [fresh() for _ in range(48)]
    entries_per_snapshot = {}
    for i in range(9):
        name = f"Replay Dictionary {i:02}"
        entries = {key: word.encode() for key, word in deep} if name == DEEPEST else {}
        if name == DEEPEST:
            # Sample density, then size the final independent dictionary to the
            # available flash. Keep room for a staged catalog and checkpoint.
            floor = len(entries)
            sample = SIZE // 4
        else:
            floor = 0
            sample = SIZE

        def fill(count):
            for index in range(len(entries), count):
                key = fresh(2 if index % 4 == 0 else 1)
                entries[key] = f"{name} independent corpus word {index:05x}".encode()

        fill(sample)
        compiled = compile_snapshot(name, entries)
        if name == DEEPEST:
            bytes_available = (256 - len(occupied(view)) - RESERVE) * SEGMENT
            fill(max(sample, bytes_available * sample // len(compiled.data)))
            while True:
                compiled = compile_snapshot(name, entries)
                if len(compiled.data) <= bytes_available:
                    break
                if len(entries) <= floor:
                    raise ValueError("The last dictionary cannot fit its replay outlines")
                remove = max(1, (len(compiled.data) - bytes_available) * len(entries) // len(compiled.data))
                for _ in range(min(remove, len(entries) - floor)):
                    entries.popitem()
        image, view = apply(image, view, plan_named_snapshot(view, occupied(view), name, compiled))
        entries_per_snapshot[name] = len(entries)

    payload = metadata_payload(view, move=(DEEPEST, 16))
    image, view = apply(image, view, Update({}, 0, payload, 0, 0))
    owners = [struct.unpack_from("<I", row)[0] for row in view.rows if row[4]]
    if len(owners) != 16 or owners[-1] != 16 or len(view.entries) != BULK_RECORDS:
        raise ValueError("Fixture requires 16 active dictionaries and 4,000 indexed records")

    # A tombstone in every higher dictionary for one deepest hit. Each active
    # dictionary also owns a distinct recent set, exercised in the paced tape.
    tail = [((owner, deep[0][0]), (1, b"")) for owner in owners[:-1]]
    tail += [((owner, key), (0, word.encode())) for owner, (key, word) in zip(owners, edited, strict=True)]
    segment = view.journals[-1][0]
    address = segment * SEGMENT + 256 + view.used[segment]
    for sequence, ((owner, key), (op, text)) in enumerate(tail, view.highwater + 1):
        record = Edit(address, owner, op, key, text)
        if address + record.span > (segment + 1) * SEGMENT:
            raise ValueError("Recent edits must fit in the existing journal tail")
        blob = encode_record(record, sequence)
        image[address:address + len(blob)] = blob
        address += record.span
    view = open_image(image)
    if view.highwater != BULK_RECORDS + len(tail):
        raise ValueError("Live edit count differs from the replay manifest")
    probes = [(render(key), word, owners[-1]) for key, word in deep]
    probes += [(render(key), word, owner) for owner, (key, word) in zip(owners, edited, strict=True)]
    probes += [(render(key), None, 0) for key in misses]
    # Javelin resegments this three-stroke suffix on the fifth chord. The
    # journal admits that length for Lapwing Commands, whose base stops at two.
    history_miss = deep[0][0] + deep[1][0] + misses[1]
    if history_miss in present:
        raise ValueError("The three-stroke regression outline must be absent")
    probes.append((render(history_miss), None, 0))
    phases, expected, events = {}, "", []
    for i, (key, word) in enumerate(deep):
        if 1 <= i <= len(misses):
            miss = render(misses[i - 1])
            events.extend(((miss, expected + " " + miss), ("*", expected)))
        edit_index = i - 1 - len(misses)
        if 0 <= edit_index < len(edited):
            edit_key, edit_word = edited[edit_index]
            expected += " " + edit_word
            events.append((render(edit_key), expected))
        expected = f"{expected} {word}" if expected else word
        events.append((render(key), expected))
    words = len(deep) + len(edited)
    for rate in RATES:
        active = round(60e9 * words / rate)
        tape = [(round(i * active / len(events)), outline, state)
                for i, (outline, state) in enumerate(events, 1)]
        phases[rate] = dict(words=words, chords=len(tape), active_ns=active, idle_ns=0,
                            duration_ns=active, expected_text=expected, events=tape)

    used = len(occupied(view)) - 1  # Exclude the last physical segment, outside the pool.
    manifest = dict(active_dictionaries=len(owners), checkpoint_records=BULK_RECORDS, recent_edits=len(tail),
                    last_dictionary_hits=len(deep), full_stack_misses=len(misses), edited_hits=len(edited),
                    used_segments=used, pool_segments=255, entries_per_snapshot=entries_per_snapshot,
                    image_sha256=hashlib.sha256(image).hexdigest())
    (out / "simulated-pool.bin").write_bytes(image)
    bundle = capture(lambda at, size: bytes(image[at:at + size]), view, "Mac U.S.")
    (out / "test-pool.json").write_text(json.dumps(bundle, separators=(",", ":")) + "\n")
    (out / "fixture.json").write_text(json.dumps(dict(**manifest, probes=probes, phases=phases), indent=2) + "\n")
    return manifest, probes, phases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("engine", type=Path, help="Pinned javelin-steno checkout")
    parser.add_argument("--out", type=Path, required=True, help="New output directory")
    parser.add_argument("--virtual", action="store_true", help="Run without wall-clock pacing")
    args = parser.parse_args()
    revision = subprocess.check_output(["git", "-C", str(args.engine), "rev-parse", "HEAD"], text=True).strip()
    manifest = (ROOT / "config/west.yml").read_text(encoding="utf-8")
    pinned = re.search(r"- name: javelin-steno\s+remote: \S+\s+revision: ([0-9a-f]{40})", manifest)
    if not pinned or revision != pinned[1] or subprocess.check_output(
            ["git", "-C", str(args.engine), "status", "--porcelain"]):
        parser.error("Use the clean pinned Javelin checkout from config/west.yml")
    args.out.mkdir(parents=True, exist_ok=False)
    manifest, probes, phases = fixture(args.out)
    print(f"Fixture uses {manifest['used_segments']}/255 segments.", flush=True)
    binary = build_engine(args.engine, args.out / "native", Path(__file__).with_name("typing_replay.cc"),
                          compiler(), optimization="-O2")
    execute(binary, args.out, {"probes": probes}, phases, args.virtual, verify_negative=True, live_tail=True)
    passed = args.virtual or all(row["deadline_overruns"] == 0 for row in phases.values())
    report = dict(**manifest, engine_commit=revision, phases=phases, exact_lookup_probes=2 * len(probes),
                  result="pass" if passed else "fail",
                  scope="Host Javelin and Native storage with simulated NOR and captured key events.")
    (args.out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
