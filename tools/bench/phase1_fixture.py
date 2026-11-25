#!/usr/bin/env python3
"""Build an offline 64 MiB qualification pool and independent typing oracle."""
import argparse
from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
import math
from pathlib import Path
import random
import struct

from run_typing_replay import ROOT
from build_native_image import LARGE_POOL_END, SEGMENT, SOURCES, build, catalog_header, seed_entries
from compile_dict import parse_outline
from hybrid import format_stroke
from native_recovery import capture, reconstruct
from native_snapshot import compile_snapshot, inspect_free, plan_named_snapshot
from native_update import EXTENT, Edit, encode_record
from snapshot_feasibility import apply, occupied, open_image

SEED = 16300400
SLOTS = 32
ACTIVE = 16
DEEP = 20
NAMES = {**{identity: name for identity, name, _, _ in SOURCES},
         **{identity: f"Phase 1 Dictionary {identity:02d}" for identity in range(8, SLOTS + 1)}}
RATES = (300, 350, 400)


def stroke_name(key):
    return "/".join(map(format_stroke, key))


def synthetic_base(identity, count, explicit):
    # Each compact has a large one-stroke table, so the known misses cannot
    # pass through a length shortcut. Separate two-stroke ranges also keep
    # the filler out of the replay's adjacent stroke combinations.
    entries = dict(explicit)
    for index in range(count - len(entries)):
        if index % 2 == 0 and index // 2 < 60_000:
            key = (0x600000 + (identity - 8) * 0x10000 + index // 2,)
        else:
            key = (0x20000 + identity * 1024 + index // 1024, 0x400000 + index % 1024)
        entries[key] = f"d{identity:02d} entry {index // 2:06x}".encode()
    return entries


def journal_blocks(entries):
    blocks, used = 1, 256
    for (identity, key), (op, text) in entries.items():
        span = Edit(0, identity, op, key, text).span
        if used + span > SEGMENT:
            blocks, used = blocks + 1, 256
        used += span
    return blocks


def fit_base(identity, count, available, protected_tail):
    best = None
    tried = set()
    for _ in range(8):
        if count in tried:
            break
        tried.add(count)
        compact = compile_snapshot(NAMES[identity], synthetic_base(identity, count, protected_tail))
        size = len(compact.data)
        if size <= available and (best is None or size > len(best[1].data)):
            best = count, compact
        if 0 <= available - size < SEGMENT:
            break
        count = max(len(protected_tail) + 1, min(750_000, count * (available - 1024) // size))
    if best is None:
        raise ValueError("Final compact cannot fit while protecting the planned reserves")
    return best


def compile_base(identity, count, explicit):
    return compile_snapshot(NAMES[identity], synthetic_base(identity, count, explicit))


def publish_fixture(image, view, active, edits, tail):
    # Reuse the original empty journal and append new journals and indexes
    # after the already staged compact images.
    journal = view.journals[0][0]
    extents = [EXTENT.pack(*extent) for extent in view.extents]
    next_segment = max(occupied(view) - {view.pool_end}) + 1
    output = bytearray(image)
    seed_entries(output, extents, journal, next_segment, edits)
    rows = [bytearray(row) for row in view.rows]
    ids = [struct.unpack_from("<I", row)[0] for row in rows]
    assert len(ids) == SLOTS and len(set(ids)) == len(ids)
    for identity, row in zip(ids, rows, strict=True):
        struct.pack_into("<I", row, 4, int(identity in active))
    rows.append(rows.pop(ids.index(DEEP)))
    prefix = bytearray(view.payload[:32])
    struct.pack_into("<HHQ", prefix, 4, len(rows), len(extents), len(edits))
    payload = bytes(prefix) + b"".join(rows) + b"".join(extents)
    for slot in (0, 1):
        start = slot * SEGMENT
        output[start:start + SEGMENT] = (catalog_header(slot + 1, payload) + payload).ljust(SEGMENT, b"\xff")
    view = open_image(output)
    last = view.journals[-1][0]
    address = last * SEGMENT + 256 + view.used[last]
    for sequence, (identity, key, op, text) in enumerate(tail, view.highwater + 1):
        entry = Edit(address, identity, op, key, text)
        if address + entry.span > (last + 1) * SEGMENT:
            raise ValueError("The qualification tail exceeds the last journal page")
        data = encode_record(entry, sequence)
        output[address:address + len(data)] = data
        address += entry.span
    return output, open_image(output)


def tape(deep_hits, edits, misses):
    events, expected = [], ""
    edited = list(edits.items())
    for index, (key, word) in enumerate(deep_hits.items()):
        if index < len(misses) and index > 0:
            caption = stroke_name(misses[index])
            events.extend(((caption, f"{expected} {caption}"), ("*", expected)))
        if index < len(edited):
            edit_key, edit_word = edited[index]
            text = edit_word.decode()
            expected = f"{expected} {text}" if expected else text
            events.append((stroke_name(edit_key), expected))
        text = word.decode()
        expected = f"{expected} {text}" if expected else text
        events.append((stroke_name(key), expected))
    words = len(deep_hits) + len(edited)
    phases = {}
    for rate in RATES:
        duration = round(60e9 * words / rate)
        schedule = [(round(i * duration / len(events)), outline, text)
                    for i, (outline, text) in enumerate(events, 1)]
        phases[rate] = dict(words=words, chords=len(schedule), active_ns=duration, idle_ns=0,
                            duration_ns=duration, expected_text=expected, events=schedule)
    return phases


def fixture(output, baseline_entries, active_edits, disabled_edits, fill):
    rng = random.Random(SEED)
    used = {(4,)}  # Reserve the undo stroke as well as the real outlines below.
    sources = ROOT / "build"
    source_info = {}
    for _, name, _, path in SOURCES[1:]:
        raw = (sources / path).read_bytes()
        parsed = json.loads(raw)
        used.update(map(parse_outline, parsed))
        source_info[name] = {"sha256": hashlib.sha256(raw).hexdigest(), "source_entries": len(parsed)}

    def fresh():
        while True:
            mask = rng.randrange(1, 1 << 23)
            if 0x20000 <= mask < 0x30000 or 0x400000 <= mask < 0x401000 or mask >= 0x600000:
                continue
            key = (mask,)
            if key not in used:
                used.add(key)
                return key

    active = {1, 6, *range(8, 22)}
    assert len(active) == ACTIVE and DEEP in active
    deep_hits = {fresh(): f"probe{i:03d}".encode() for i in range(168)}
    misses = [fresh() for _ in range(49)]
    tail_edits = {identity: (fresh(), f"edited{identity:02d}".encode()) for identity in sorted(active)}
    edits = {(identity, fresh()): (0, f"journal {identity:02d} entry {index:04d}".encode())
             for identity in NAMES
             for index in range(active_edits if identity in active else disabled_edits)}
    # Overlapping disabled records, active tombstones, and reset to a base.
    priority_probe = next(iter(deep_hits))
    for identity in NAMES.keys() - active:
        edits[identity, priority_probe] = (0, b"disabled override")
    for identity in active - {DEEP}:
        edits[identity, priority_probe] = (1, b"")
    reset_probe = list(deep_hits)[1]
    edits[DEEP, reset_probe] = (1, b"")
    tail = [(DEEP, reset_probe, 2, b"")]
    tail.extend((identity, key, 0, word) for identity, (key, word) in tail_edits.items())

    new_journals = journal_blocks(edits) - 1
    index_blocks = math.ceil(4 * (len(edits) + len(tail)) / SEGMENT)
    # Retain the mandatory three blocks plus room for another pair of
    # indexes, one rotation, metadata and the small consolidation fixture.
    headroom = 2 * index_blocks + 6
    image, factory = build(sources, pool_end=LARGE_POOL_END)
    view = open_image(image)
    manifest = {}
    with ProcessPoolExecutor(max_workers=4) as compiler_pool:
        pending = {
            identity: (count, compiler_pool.submit(compile_base, identity, count,
                                                   deep_hits if identity == DEEP else {}))
            for identity in range(8, SLOTS)
            for count in (128 if identity == SLOTS - 1 else baseline_entries,)
        }
        for identity, (count, compiled) in pending.items():
            image, view = stage_base(image, view, identity, count, compiled.result(), manifest)
        if fill:
            free, longest = inspect_free(occupied(view), view.pool_end)
            allowed = min(free - new_journals - 2 * index_blocks - headroom - 1, longest)
            if allowed <= 0:
                raise ValueError("Earlier compacts leave no space for the final dictionary")
            count, compiled = fit_base(SLOTS, max(baseline_entries, 3 * allowed * SEGMENT // 64),
                                       allowed * SEGMENT, {})
        else:
            count = baseline_entries
            compiled = compile_base(SLOTS, count, {})
        image, view = stage_base(image, view, SLOTS, count, compiled, manifest)

    image, view = publish_fixture(image, view, active, edits, tail)
    free, longest = inspect_free(occupied(view), view.pool_end)
    if free < headroom or longest < headroom:
        raise ValueError(f"Only {free} free blocks and {longest} contiguous blocks remain")
    active_names = [NAMES[struct.unpack_from("<I", row)[0]] for row in view.rows if row[4]]

    bundle = capture(lambda at, size: bytes(image[at:at + size]), view, "Mac U.S.")
    restored, verified = reconstruct(bundle)
    if restored != image or view.highwater != verified.highwater:
        raise ValueError("Offline recovery round-trip differs")
    probes = [(stroke_name(key), text.decode(), DEEP) for key, text in deep_hits.items()]
    probes += [(stroke_name(key), word.decode(), identity) for identity, (key, word) in tail_edits.items()]
    probes += [(stroke_name(key), None, 0) for key in misses]
    (output / "simulated-pool.bin").write_bytes(image)
    (output / "test-pool.json").write_text(json.dumps(bundle, separators=(",", ":")) + "\n")
    report = dict(seed=SEED, pool_segments=view.pool_end, used_segments=view.pool_end - free,
                  free_segments=free, largest_free_run=longest, provisioned_slots=len(view.rows),
                  installed_dictionaries=len(view.rows), active_dictionaries=len(active),
                  active_names=active_names, checkpoint_records=len(edits), recent_edits=len(tail),
                  source_info=source_info, base_images=factory["base_images"], compiled=manifest,
                  extents=[dict(start=start, blocks=count, size=size, kind=kind, crc=crc)
                           for start, count, size, crc, kind, _ in view.extents],
                  image_sha256=hashlib.sha256(image).hexdigest(), stages_reserved=headroom,
                  full_stack_misses=[stroke_name(key) for key in misses],
                  probes=probes, phases=tape(deep_hits, {key: word for key, word in tail_edits.values()}, misses))
    (output / "fixture.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"Verified {report['used_segments']}/{view.pool_end} occupied blocks; {free} free, {longest} contiguous.", flush=True)


def stage_base(image, view, identity, count, compiled, manifest):
    update = plan_named_snapshot(view, occupied(view), NAMES[identity], compiled, limit=SLOTS)
    image, view = apply(image, view, update)
    scratch = update.metadata * SEGMENT
    image[scratch:scratch + SEGMENT] = b"\xff" * SEGMENT
    start, blocks, size, *_ = view.extents[-1]
    manifest[NAMES[identity]] = dict(id=identity, entries=count, address=start * SEGMENT,
                                    bytes=size, blocks=blocks)
    print(f"Staged {NAMES[identity]}: {count:,} entries, {blocks} blocks, start 0x{start * SEGMENT:08x}", flush=True)
    return image, view


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--baseline-entries", type=int, default=100_000)
    parser.add_argument("--active-edits", type=int, default=4_000)
    parser.add_argument("--disabled-edits", type=int, default=1_000)
    parser.add_argument("--skip-fill", action="store_true", help="Use for a small host smoke fixture")
    args = parser.parse_args()
    if args.baseline_entries < 168 or min(args.active_edits, args.disabled_edits) < 1:
        parser.error("Need at least 168 baseline entries and one journal entry per dictionary.")
    args.out.mkdir(parents=True, exist_ok=False)
    fixture(args.out, args.baseline_entries, args.active_edits, args.disabled_edits, not args.skip_fill)


if __name__ == "__main__":
    main()
