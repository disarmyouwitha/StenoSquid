# Candidate B: native catalog, checkpoint construction and dictionary bridge

Status: **native components implemented; publication/reboot/recovery hardware test passed**.
This is P3 development on `prototype/hybrid-overlay-journal`, not a production
format switch. No permanent migration readers, compatibility builds or changes to
the default firmware's dictionary commands are introduced.

## Binary catalog

`tools/storage/native_catalog.h` streams metadata through fixed workspace rather
than loading a JSON catalog into RAM. It supports up to 64 logical dictionaries
and 253 extent descriptors on the current 16 MiB map. Wire fields are explicitly
little-endian; no serialized C++ structs or native pointers.

| Structure | Bytes | Fields |
| --- | ---: | --- |
| Commit envelope | 32 | `SQBCAT02`, generation, payload bytes/CRC, header CRC, final commit word |
| Payload prefix | 32 | Next stable ID, dictionary/extent counts, checkpoint sequence, reserved zeroes |
| Dictionary row | 96 | ID, enabled flag, optional base extent index, UTF-8 name (1–44 bytes), source SHA-256 |
| Extent row | 24 | Start/count in 64 KiB segments, used bytes, content CRC, kind, journal epoch |

Dictionary table order determines priority. IDs cannot repeat or reach the next
unallocated ID. Publication cannot lower the next ID or resurrect a previously
deleted ID; new dictionaries must use newly allocated identities. This prevents
old overlay records from attaching to a different dictionary. Extents cannot
overlap each other, catalogs or the reserved tail.
Base extents have exactly one dictionary owner; grouped ownership is deliberately
unsupported. At most one forward and reverse checkpoint root is allowed. The
caller must also validate actual content, index ordering/coverage and journal
references through the extent callback; schema validation alone does not establish
that an image or index is safe to use.

Publication preflights the serialized source and current root before erase. It
stages the alternate catalog, verifies readback and schema/extents, then commits
last. Stale callers cannot overwrite a newer committed root. Initial publication
refuses nonblank catalog regions. Opening never formats or repairs. Corrupt
committed catalogs cause read-only quarantine/fallback or refusal; unknown and
unfinished first-install bytes require explicit provisioning recovery, not an
automatic erase. Resuming interrupted first provisioning is not implemented yet.

Four bounded reader-pin slots protect the **catalog slot itself**, not just its
referenced dictionary data. Recycling a pinned target returns `Busy` before any
write. Pin tokens prevent double release; pin collection exposes roots whose
extents an allocation plan must protect. The store must serialize these calls,
release cancelled exports, and defer/retry publication until readers finish.
Two slots therefore do not provide unlimited metadata updates during a long-held
reader. This is an explicit backpressure policy rather than a hidden RAM copy.
Preflight schema validation reads directly from its supplied source, sharing the
same streaming validator as flash recovery. It does not construct a temporary
catalog object with another workspace and reader registry. In the local ARM
stack report, the publication frame fell from 912 to 568 bytes; nested validation,
device callbacks, HID code and interrupts still add to the actual stack peak.

## Incremental checkpoint construction

`tools/storage/native_checkpoint.h` merges an immutable old checkpoint with at
most 256 chronological recent edits. It uses two fixed address arrays, not a
dictionary-sized map. Stable forward sorting retains the latest equal key;
remove records remain forward tombstones; reset removes the old override. Reverse
construction drops any old row superseded by the recent tail and inserts only
current set records, ordered by text plus dictionary ID/outline.

Each `Step()` emits at most one address. Insertion sorting advances one
comparison/shift at a time. Reverse filtering uses at most a logarithmic search
of 256 recent keys; key/text comparisons remain bounded by record limits. The
runtime caller can yield between steps, but the adapter's I/O and long-text
comparison costs still require measurement. Operation-count bounds are not a
guarantee of a 1 ms step or short flash-lock hold time.

`native_index_sink.h` buffers one 256-byte output page, verifies programmed data
in 32-byte readback chunks, and accumulates a CRC/count. It writes only to a
caller-authorized, erased, preallocated extent. Completing a sink does not publish
an index. Both new tables must finish and pass semantic validation before the
catalog pointer switch. The allocator must protect old roots, retained catalogs,
readers and all staged output capacity throughout that transaction.

Checkpoint rebuilding still writes replacement index tables. It has bounded RAM
and step work, but is not a journal cleaner or a complete near-full maintenance
policy. No capacity or write-amplification acceptance claim follows.

## Javelin adapter

`src/steno_squid/native_dictionary_adapter.h` implements the actual pinned
Javelin `StenoDictionary` lookup interface. Its backend acquires a catalog pin,
returns an overlay result or base fallback, and copies set text into a precisely
sized RAM result before releasing the pin. Tombstones return no result from that
logical dictionary so lower-priority dictionaries may win; reset/missing uses
that dictionary's base. Backend base results must likewise own stable RAM/ROM
text rather than return a reclaimable QSPI pointer.

The isolated diagnostic uses this bridge against its native index. It is **not**
registered in the everyday engine collection. Native compact-base loading,
reverse hints/export through the adapter, catalog-driven
enable/priority updates, editor operations and full-engine publication remain
integration work. Native live-tail snapshots and recovery are now implemented on
`prototype/native-live-store`; see the [next milestone](hybrid-native-live-store.md).
Backend errors must not be mistaken for normal absence when
the eventual store decides overall engine readiness.

## Tests so far

- Native catalog: 344 torn replacement-publication cases (one and 64 dictionaries),
  plus 76 torn first-provisioning cases. Old/new contents and read-only recovery
  checked. Initial partial writes are refused rather than silently formatted.
- Combined native transaction: 212 torn staging/index/catalog cases verify that
  recovery selects a complete old/new index pair and preserves all committed edits.
- Index sink: 16 torn-page cases, page/readback CRC checks and capacity refusal.
- Catalog semantic checks: duplicate IDs, names, base ownership, bad extent
  callback, stale roots, I/O failures, quarantine, unknown data and bounded pins.
- Native merge: 181 independent map-oracle cases, including 200,000 old records
  and 256 edits. The run used **2,104,876** bounded steps across all cases.
- Adapter: UTF-8, empty/2,047-byte text, pinned-copy lifetime, reset/base fallback,
  tombstone/error suppression, invalid outlines and failed reads.
- Actual hardware-handler source compiled with strict DMA/sector/page doubles:
  two catalog generations, old-index/recent-edit merge, cold-state reopen,
  Javelin adapter lookup, cleanup and quarantine refusal.

All new tests are discovered by the native runner. Linux CI sanitizes the native
core and actual handler tests with ASan/UBSan and aborts on findings. Mac UBSan
is also run; local ASan remains unavailable as documented in the earlier core
milestone. Firmware CI verifies the bridge against actual pinned Javelin headers;
the small host interface double checks ownership/semantics separately.

## On-device checkpoint experiment

`dictionary_checkpoint_probe` is USB-only and Left-Diagnostic-only. It exposes
exactly seven reserved 4 KiB sectors at `0xFF9000..0xFFFFFF`, mapping them to
virtual catalog slots, one test journal and old/new index extents. These are
isolated test sectors, not new production partitions.

- `hold`: refuse nonblank space; append six records; build/publish checkpoint 1;
  append three edits; incrementally merge old tables and publish checkpoint 2.
- `check`: reopen persistent catalog/records/indexes and verify the second
  generation and the Javelin dictionary adapter result. Performs no writes.
- `clean`: erase only after the complete known test state verifies, then check
  the entire reserved test region is FF. Failure/corruption never triggers force
  erase. Interrupted setup/cleanup may require explicit diagnostic recovery.
- `run`: prepare, verify and clean in one session.
- `reboot`: acknowledge, then schedule a normal reboot after two seconds.

The host command prepares, reboots, reconnects, verifies persistence, cleans,
repeats, and compares production dictionary lookup/export:

```sh
python3 tools/bench/run_checkpoint_probe.py --serial USB_SERIAL \
  --output build/checkpoint-probe/hardware.json
```

The diagnostic intentionally holds the engine lock during synchronous I/O; do not
type during it. It does not exercise the future background scheduler or typing
under maintenance. Personal backups and unique serials remain local.

## Boardsource hardware results — 2026-11-18

The exact GitHub Actions artifacts from
[run 44333531690](https://github.com/disarmyouwitha/StenoSquid/actions/runs/44333531690)
at `8ea690c` were flashed: `Boardsource-Left-Diagnostic` and
`Boardsource-Right-Default`. All twelve firmware/probe builds, native tests and
Linux ASan/UBSan checks passed. The diagnostic reported the expected build ID,
Winbond `EF 40 18` and 2 MHz QSPI.

The automated sequence passed:

| Operation | Observed result |
| --- | --- |
| Prepare records, build initial and replacement tables, publish two roots | Passed; 59 builder steps; 434 ms |
| Normal software reboot | Acknowledged; USB HID reconnected |
| Recover from flash with warm state discarded | Generation 2 and expected Javelin adapter result; 15 ms |
| Verify then clear all seven test sectors | Passed, all FF; 373 ms |
| Repeat prepare/verify/cleanup | Passed; 59 steps; 811 ms |

Elapsed times use millisecond uptime, not the separate DWT lookup benchmark.
The observed HID stack free watermark was **1,604 bytes** after preparation,
versus 2,660 before. After reboot/repeat it reported 1,708 bytes free. These are
observed workload watermarks, not worst-case guarantees or heap measurements.

The installed left diagnostic uses **533,928 bytes of internal flash** and
**229,918 bytes of static RAM** (87.71% of 256 KiB). This includes Studio BLE,
existing diagnostics/battery logging and both storage test harnesses; it is not
the default firmware's memory cost. The right default uses 442,748 flash bytes
and 88,928 RAM bytes. Default firmware does not compile these diagnostic helpers.

KAT lookup and personal export matched before/after. Production storage remained
format 3, ready, with zero integrity errors. Full base readback CRCs passed for
Lapwing `00f2d636`, Plover `3e8f8d88` and extras `98af1ecc`. The right accepted its
UF2 and returned from bootloader; hands-on split typing, display and Practice
tests are deferred until the owner returns.

The [hardware report](benchmarks/candidate-b-native-checkpoint.json) includes
artifact hashes, phase results, stack observations and preservation checks. The
[native test report](prototypes/hybrid-native-catalog-checkpoint.json) records
fault counts, the large merge case and source hashes. Neither includes personal
entries or unique USB serials.

## Remaining work

A full store still needs transaction orchestration and ownership collection,
live-tail snapshot/recovery, incremental journal cleaning and measured near-full
staging, relocatable compiled-base engine loading, hints/export/editor integration,
host provisioning/update commands, and hardware speed/power-cut validation.
The complete CLI/Manager and release phases remain ahead. Default firmware stays
on the working current store until those gates are met.
