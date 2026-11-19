#!/usr/bin/env python3
"""Compare real Native staged journals with compiled JSC4 snapshot plans."""
import argparse
import contextlib
import csv
import hashlib
import io
import json
from pathlib import Path
import struct
import sys
import time
import zlib

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools" / sub) for sub in ("storage", "dictionaries", "dict_compiler")]
from build_native_image import SEGMENT, XIP, build, catalog_header
from hybrid import compact_entries, format_stroke
from import_personal import parse_named
from native_base_update import PINS
from native_snapshot import compile_snapshot, plan_named_snapshot
from native_update import NoSpace, Snapshot, plan


def open_image(image, payload=None):
    address = SEGMENT + 32
    if payload is None:
        length, crc = struct.unpack_from("<II", image, address - 16)
    else:
        length, crc = len(payload), zlib.crc32(payload)
    return Snapshot(lambda start, length: image[start:start + length], address, length, crc)


def occupied(view):
    return {0, 1, 255} | {i for base, count, *_ in view.extents for i in range(base, base + count)}


def longest_free(used):
    maximum = length = 0
    for segment in range(2, 255):
        length = 0 if segment in used else length + 1
        maximum = max(maximum, length)
    return maximum


def apply(image, view, update):
    output = bytearray(image)
    for segment, data in update.segments.items():
        assert segment not in occupied(view)
        at = segment * SEGMENT
        output[at:at + SEGMENT] = bytes(data).ljust(SEGMENT, b"\xff")
    for slot in (0, 1):
        at = SEGMENT * slot
        output[at:at + SEGMENT] = (catalog_header(3 + slot, update.payload) + update.payload).ljust(SEGMENT, b"\xff")
    return output, open_image(output, update.payload)


def synthetic(n):
    result = {(i // 1024 + 1, i % 1024 + 1): f"translation {i % 3000}".encode() for i in range(n)}
    source = {"/".join(map(format_stroke, key)): text.decode() for key, text in result.items()}
    assert parse_named(json.dumps(source))[0] == result
    return result


def measure(label, entries, image, base, mode, compiled):
    start = time.perf_counter()
    name = "Experiment"
    def build_plan(view, used, name):
        return plan(view, used, entries, name=name) if mode=="J" else plan_named_snapshot(view, used, name, compiled)
    result = dict(corpus=label, mode=mode, entries=len(entries), input_text_bytes=sum(map(len, entries.values())),
                  image_bytes=len(compiled.data) if mode=="S" else None,
                  base_free_run=longest_free(occupied(base)))
    if mode=="J":
        # Exact sequential record packing and 4-byte index entries, also when
        # the allocator refuses before returning a complete staged image.
        from native_update import Edit
        journals, position, record_bytes = 1, 256, 0
        for key, value in entries.items():
            span = Edit(0, 8, 0, key, value).span
            if position + span > SEGMENT:
                journals, position = journals + 1, 256
            position += span
            record_bytes += span
        indexes = 2 * ((4 * len(entries) + SEGMENT - 1) // SEGMENT)
        result.update(record_bytes=record_bytes, journal_segments=journals, index_segments=indexes,
                      estimated_rebuild_used=len(occupied(base)) - 1 + journals + indexes)
    try:
        update = build_plan(base, occupied(base), name)
    except NoSpace as error:
        result.update(install=False, refusal=str(error))
        return result
    data, after = apply(image, base, update)
    if mode=="J":
        assert {key: entry.text for (identity, key), entry in after.entries.items() if identity==8} == entries
    else:
        start_segment, _, size, _, _, _ = after.extents[-1]
        at = start_segment * SEGMENT
        actual = {outline: text for outline,text in compact_entries(data[at:at+size], XIP+at)}
        expected = {"/".join(map(format_stroke, key)): value.decode() for key,value in entries.items()}
        assert actual == expected
    used = occupied(after)
    result.update(install=True, verified=len(entries), steady_segments=len(used)-1,
                  added_segments=len(used)-len(occupied(base)), initial_stage_segments=len(update.segments),
                  rebuild_segments=len(used)-1, rebuild_image_bytes=len(data),
                  replacement_free_run=longest_free(used))
    try:
        replacement = build_plan(after, used, "Replacement")
        result.update(replacement_stage_segments=len(replacement.segments),
                      replacement_peak_segments=len(used | set(replacement.segments))-1)
    except NoSpace as error:
        result.update(replacement_refused=str(error))
    result.update(host_seconds=round(time.perf_counter()-start,3))
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--full", action="store_true")
    args=parser.parse_args()
    hashes={}
    cases=[]
    for label in ("Plover Base", "Lapwing Base"):
        _, file, digest, directory=PINS[label]
        raw=(ROOT/"build"/directory/file).read_bytes()
        assert hashlib.sha256(raw).hexdigest()==digest
        hashes[label]=digest
        if label=="Plover Base" or args.full:
            cases.append((label,parse_named(raw)[0]))
    cases.append(("legal-100",synthetic(100)))
    if args.full:
        cases.extend((f"legal-{n}",synthetic(n)) for n in (500,1000,5000,10000,50000,100000,190000,200000))
        cases.append(("dense-6900", {(0x400000|n,): f"temporary storage test {n:05d} ".ljust(1120,"x").encode()
                                       for n in range(1,6901)}))
    image,_=build(ROOT/"build")
    base=open_image(image)
    runs=[]
    for label, entries in cases:
        if label=="dense-6900":
            with contextlib.redirect_stderr(io.StringIO()):
                compiled=compile_snapshot("Experiment",entries, search=False)
        else:
            compiled=compile_snapshot("Experiment",entries, search=False)
        for mode in ("J","S"):
            row=measure(label,entries,image,base,mode,compiled)
            print(json.dumps(row),flush=True)
            runs.append(row)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(dict(source_sha256=hashes,base_segments=len(occupied(base))-1,
                                            three_segment_reserve=3, measurements=runs),indent=2)+"\n")
    with args.output.with_suffix(".csv").open("w",newline="") as output:
        columns=list(dict.fromkeys(k for row in runs for k in row))
        writer=csv.DictWriter(output,columns)
        writer.writeheader()
        writer.writerows(runs)

if __name__=="__main__":
    main()
