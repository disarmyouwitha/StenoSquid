#!/usr/bin/env python3
"""Measure SQS1 sections and alternative encodings without changing firmware."""
import argparse
import hashlib
import json
from pathlib import Path
import struct

from snapshot_feasibility import ROOT, synthetic
from build_native_image import SEGMENT
from compact_search_index import FOOTER, MAGIC, add_search_index
from import_personal import parse_named
from native_base_update import PINS
from native_snapshot import compile_snapshot


def segments(size):
    return (size + SEGMENT - 1) // SEGMENT


def measure(entries):
    data = add_search_index(compile_snapshot("user_dictionary", entries).data)
    magic, legacy, reverse, buckets, postings = FOOTER.unpack_from(data, len(data) - FOOTER.size)
    assert magic == MAGIC and reverse == len(entries)
    hint_magic, hint_start, hint_members = struct.unpack_from("<4sII", data, legacy - 12)
    assert hint_magic == b"SHI1" and hint_members == 1
    hint_offset, hint_count = struct.unpack_from("<II", data, hint_start)
    text_bytes, text_offset = struct.unpack_from("<II", data, 8)
    unpadded = text_bytes + text_offset + 4
    padding = -unpadded % 4
    assert unpadded + padding == hint_offset
    assert hint_offset + 8 * hint_count == hint_start
    assert legacy == hint_start + 8 + 12
    parts = dict(jsc4_through_timestamp=unpadded, legacy_alignment=padding,
                 legacy_hint_records=8 * hint_count, legacy_hint_directory=8, legacy_hint_footer=12,
                 reverse_text=4 * reverse, partial_buckets=8 * buckets, partial_postings=4 * postings,
                 sqs1_alignment=0, sqs1_footer=FOOTER.size)
    assert sum(parts.values()) == len(data)
    bucket_address = legacy + reverse * 4
    posting_address = bucket_address + buckets * 8
    unique = set()
    delta_bytes = 0

    def varint_size(value):
        return max(1, (value.bit_length() + 6) // 7)

    for i in range(buckets):
        start = struct.unpack_from("<I", data, bucket_address + i * 8 + 4)[0]
        end = postings if i + 1 == buckets else struct.unpack_from("<I", data, bucket_address + i * 8 + 12)[0]
        block = data[posting_address + start * 4:posting_address + end * 4]
        unique.add(block)
        previous = 0
        for (token,) in struct.iter_unpack("<I", block):
            change = token - previous
            delta_bytes += varint_size(2 * change if change >= 0 else -2 * change - 1)
            previous = token

    dictionary = struct.unpack_from("<I", data, 36)[0]
    table = struct.unpack_from("<I", data, dictionary + 12)[0]
    maximum_group = 0
    for group in range(data[dictionary + 1]):
        mask, _, bitmap = struct.unpack_from("<III", data, table + group * 12)
        if mask:
            maximum_group = max(maximum_group, sum(
                struct.unpack_from("<I", data, bitmap + block * 20 + word * 4)[0].bit_count()
                for block in range((mask + 1) // 128) for word in range(4)))
    assert maximum_group <= 1 << 20
    rank24 = legacy + 3 * (reverse + postings) + 6 * buckets + FOOTER.size
    assert postings < 1 << 24

    def candidate(size):
        # Rounded once before a conceptual aligned footer. No format is emitted.
        size += -size % 4
        return dict(bytes=size, segments=segments(size), saved_bytes=len(data) - size,
                    saved_segments=segments(len(data)) - segments(size))

    return dict(entries=len(entries), largest_stroke_group=maximum_group, reverse_records=reverse,
                partial_buckets=buckets, partial_postings=postings, unique_posting_lists=len(unique),
                parts=parts, no_search_bytes=legacy, no_search_segments=segments(legacy),
                search_bytes=len(data) - legacy, indexed_bytes=len(data), indexed_segments=segments(len(data)),
                segment_padding=segments(len(data)) * SEGMENT - len(data),
                experimental_estimates={
                    "packed_24bit_rank_tokens_and_bucket_fields": candidate(rank24),
                    "deduplicate_with_explicit_bucket_lengths":
                        candidate(legacy + 4 * reverse + 12 * buckets + sum(map(len, unique)) + FOOTER.size),
                    "signed_leb128_posting_deltas":
                        candidate(legacy + 4 * reverse + 8 * buckets + delta_bytes + FOOTER.size),
                })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = (ROOT / "build/plover/plover-main.json").read_bytes()
    source_sha = hashlib.sha256(raw).hexdigest()
    assert source_sha == PINS["Plover Base"][2]
    report = {"segment_bytes": SEGMENT, "plover_source_sha256": source_sha,
              "pinned-plover": measure(parse_named(raw)[0]), "legal-200k": measure(synthetic(200000))}
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
