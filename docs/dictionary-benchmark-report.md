# Dictionary storage Phase-0 measurements

Status: **first host measurements complete; read-only hardware baseline prepared**.
No architecture has been selected and no new on-flash format is implemented.
See [the commands and measurement limits](../tools/bench/README.md).

## Reproducible host results

Command: `python3 tools/bench/dictionary_footprint.py`.
Inputs are the hash-pinned sources in `tools/dictionaries/manifest.py`; the
output JSON records source paths/hashes, firmware checkout, Python version,
compiler source hash and the west manifest/Javelin revision. Every source lookup
was verified against the measured compact images.

| Quantity | Measured bytes/count |
| --- | ---: |
| Source entries, with dictionaries kept separate | 291,403 |
| Canonical outlines present in multiple dictionaries | 35,362 |
| Current grouping: two bases and grouped Extras | 4,639,876 bytes |
| All six dictionaries in separate compact images | 4,640,048 bytes |
| All six in one deduplicated compact collection | 4,178,732 bytes |
| Current-format packed records, before indexes/IDs/slack | 8,184,856 bytes |

Splitting Extras costs only **172 bytes** for this pinned corpus. Combining all
six saves **461,144 bytes** versus the current grouping, but that does not itself
provide independent replacement or editable dictionaries. These images are
footprint experiments, not replacements for the installed images.

## Allocator estimates — not device measurements

A simple shared-log estimate adds four dictionary-ID bytes per record, two
shared four-byte-slot indexes, two catalog segments and three reserved GC
segments. It packs records at 64 KiB boundaries with 16-byte headers. The data
area is `0x000000..0xFEFFFF` (16,711,680 bytes), excluding the reserved tail.

| Target index occupancy | Allocated bytes incl. catalog/GC reserve | Remaining bytes |
| --- | ---: | ---: |
| 50% | 14,417,920 | 2,293,760 |
| 70% | 13,107,200 | 3,604,480 |
| 87.5% | 12,451,840 | 4,259,840 |

These do **not** yet reserve a full old/new update staging area, establish safe
GC behaviour, serialize the proposed multi-dictionary format or measure its
lookup cost. The high-occupancy alternatives have different collision/churn
risks. Even the densest estimate uses about **7.81 MB more** than today's
compiled base bytes, making Candidate A's footprint gate look difficult.
This comparison is not the footprint of a completed Candidate B/C implementation.

Individually aligned compact extents total **4,849,664 bytes**. The tool lists
catalog, empty-overlay, GC and largest-image staging assumptions separately for
B; it does not claim those extents remain allocatable after fragmentation.
Synthetic 50k/100k/190k/200k scenarios use explicitly uniform 32-byte records;
these are analytical growth cases, not demonstrated personal-dictionary capacity.

## Hardware baseline

The diagnostic-only USB command and host runner collect forward/reverse/stack
lookup timings plus actual key-path traces. They leave dictionaries and settings
unchanged. Metrics include raw DWT cycles, p50/p95/p99/max, software XIP word
loads, personal adapter read counts/bytes, JEDEC ID, QSPI speed, compiler/build
identity, current dictionary selections and the HID stack watermark.

Hardware results are **pending flashing and execution**. There are no latency,
power or zero-loss conclusions yet. Lookup timings exclude USB reporting;
real-typing trace durations include engine/hint processing but exclude event
queue wait and eventual host delivery. See the tool README for exact boundaries.

## Decision record

**Pending.** First run the existing-layout baseline on the nRF52840, then decide
how much Candidate A prototyping remains justified by its footprint. Candidate C
still requires explicitly relaxing the pooled-capacity goal. No migration reader,
compatibility shim or automatic destructive reprovisioning is part of this work.
