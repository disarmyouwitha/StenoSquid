# Dictionary storage Phase-0 measurements

Status: **host footprints and first read-only hardware baseline measured; Phase 0 remains open**.
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

The corrected diagnostic image `52f363c` passed all 12 firmware/probe CI builds
and native tests, then ran on the nice!nano prototype with Winbond `EF 40 18`,
2 MHz QSPI and the 64 MHz DWT counter. Its 86 benchmark cases completed without
lookup-parity failures. The three personal entries matched the pre-flash backup.

| Sampled forward hits | Timed lookups | Median | p99 | Maximum |
| --- | ---: | ---: | ---: | ---: |
| Lapwing Base | 288 | 297.4 µs | 614.4 µs | 683.0 µs |
| Plover Base | 288 | 298.3 µs | 770.0 µs | 775.2 µs |
| Commands | 128 | 286.0 µs | 398.5 µs | 399.6 µs |
| Numbers | 288 | 256.5 µs | 473.3 µs | 474.6 µs |
| UK Additions | 288 | 305.5 µs | 641.9 µs | 707.0 µs |
| Proper Nouns | 288 | 273.8 µs | 567.5 µs | 608.2 µs |
| Personal: three existing entries | 96 | 280.5 µs | 412.6 µs | 412.6 µs |

These are repeated timings of a small stratified corpus, not percentiles over
all entries. The almost-empty personal index is not representative of a large
Candidate A store. Of the sampled misses, the Plover miss was the slowest
(maximum **868.2 µs**); do not extrapolate the faster misses to every hash path.
The existing compiled reverse API supplies single-stroke shortcut hints and
applies enabled-priority filtering; its results are not equivalent to enumerating
all personal-log reverse matches. The HID thread retained **2,032 bytes** of
unused stack at its observed low-water mark during this baseline.

### Real typing smoke test

A 20-second manual Javelin trace recorded **21 completed chords**, including two
multi-stroke sequences, with **zero recorded drops, zero ignored chords and zero
trace overflow**. The user provided editor output containing normal text and
accents. The passage was free-form, so exact intended-text parity is not asserted.

Elapsed final-release handler time was **37.69 ms median, 48.58 ms p95 and
49.95 ms maximum**. This scope includes the engine, hints, output calls, lock and
interrupt/preemption time. It is not isolated CPU time or dictionary lookup time,
and excludes event-queue delay and eventual host delivery. ZMK's output paths
can wait for USB completion or BLE queue space; the individual contributions
have **not** yet been measured. Split stage timings and record the selected
output transport before attributing the gap or changing performance behaviour.

A small, manually paced sample with no recorded drops is not the controlled
200 WPM / 300–400 WPM replay gate. Near-full log, churn/GC contention, wake,
exact output comparison and current measurements remain outstanding.

Public raw timing samples, source pins and assumptions are checked in as
[hardware JSON](benchmarks/phase0-hardware-baseline.json),
[footprint JSON](benchmarks/phase0-footprint.json) and
[footprint CSV](benchmarks/phase0-footprint.csv). Full local reports, personal
backup and trace outlines remain under ignored `build/dictionary-phase0/`.

## Decision record

**Pending.** First run the existing-layout baseline on the nRF52840, then decide
how much Candidate A prototyping remains justified by its footprint. Candidate C
still requires explicitly relaxing the pooled-capacity goal. No migration reader,
compatibility shim or automatic destructive reprovisioning is part of this work.


## First hardware smoke test

The CI diagnostic image `06e4cdb` booted on nice!nano, reported Winbond JEDEC
`EF 40 18`, 2 MHz QSPI and a working 64 MHz DWT timer. All three personal entries
matched the pre-flash backup. Initial sampled forward lookups passed content
checks, but the run was stopped: the test's fixed 16-stroke miss exceeded one
compact dictionary's maximum. Direct compact lookup assumes the caller already
checks that bound. This is a benchmark-harness bug; the normal engine dispatcher
already filters by dictionary outline length.

The harness now checks that precondition explicitly, has a native regression
for an over-length query, and generates valid one-stroke misses for actual hash
miss measurements. The incomplete first run is not a benchmark acceptance result.
The corrected CI image passed that exact over-length query on hardware with
zero flash reads, and the full read-only baseline was then rerun successfully.
Only the corrected run supplies the table above.
