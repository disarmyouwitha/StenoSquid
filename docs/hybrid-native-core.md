# Candidate B: bounded native journal and allocation planner

Status: **native core development for P3**, not linked to keyboard firmware and
not a frozen on-flash format. This follows the
[index publication/reverse model](hybrid-index-publication.md). Installed data,
main, installer commands and dictionary selection are unchanged. No migrations.

## Implemented core

`tools/storage/native_journal.h` implements the binary journal segment/record
encoding in C++17 through a supplied `Read`/`Program`/`Erase` device interface.
There are no Zephyr dependencies, dynamic allocations or dictionary-sized mirrors.

- One 256-byte member workspace streams checksums and payload validation.
- Appends validate ID, operation, 1–16 nonzero 25-bit strokes, UTF-8, NUL rejection
  and the 2,047-byte translation limit before any write. Set with empty text,
  remove and reset remain distinct.
- Header/payload writes split at 256-byte program-page boundaries. The entire
  prospective record must be blank before programming. Exact header readback and
  streamed payload CRC verification precede the separate final commit word.
- Reads validate committed header, sizes, checksums, stroke masks and UTF-8.
  Committed corruption, I/O error, blank end and uncommitted/sealed tail are
  separate outcomes. Readers never erase, repair or format.
- A recovery cursor validates the catalog-supplied segment epoch, then processes
  one record per call and checks strictly increasing sequence numbers. Callers
  can yield/cancel between records. An I/O error leaves its position unchanged.
- Explicit `Initialize` erases only a caller-authorized segment and writes its
  epoch header. It does not allocate, publish ownership or make a new segment
  active. Unknown storage is never initialized by opening it.

`tools/storage/native_allocator.h` supplies a 32-byte segment bitmap and a private
32-byte first-fit allocation plan for the existing **16 MiB** development map.
Its input is the union of current roots, retained roots (including quarantined
ones), pinned readers and pending protected extents. It always excludes the two
catalog segments and reserved tail. Planning retains three free segments and
refuses fragmentation/capacity failures without changing the plan. It performs
no flash I/O and does not decide when an extent is safe to reclaim.

The journal has a single caller-owned writer; the caller must preflight recent
tail/index capacity, authorize and pin extent ownership, supply monotonically
increasing sequences, hold the flash/engine synchronization needed for a write,
and reconstruct writer state after any failure. Reusing a sealed tail is refused.
Input text/strokes must remain stable for the duration of Append. Flash failures
after the commit may have committed the edit even if readback fails; command
idempotency must resolve that ambiguity at the integration layer.

## Evidence

`tools/tests/test_native_journal.py` creates segment/records with the independent
Python implementation, reads them through the native implementation, appends a
native record, and validates the resulting bytes through Python again. It covers
multi-stroke outlines, extended stroke bits, UTF-8, tombstones and reset records.

The native fake-NOR harness checks:

- **3,916** torn append cases, using all 64 four-byte-aligned page offsets, empty
  and maximum-length text, and four torn-prefix lengths per program operation.
- **68** torn explicit-initialization cases across sector erase/header programs.
- Recovery performs no writes and recognizes only complete, blank or sealed
  append outcomes. CRC-consistent invalid UTF-8 is still rejected as corruption.
- Bad readback cannot publish a commit; occupied locations and invalid requests
  are refused. Scan sequence regression, epoch mismatch and I/O failure are tested.
- **160,000** deterministic allocation requests against an independent boolean
  first-fit model, plus fragmented free space, pinned extents, reserve exhaustion
  and invalid-range refusal.

The freestanding Cortex-M4 probe in `tools/storage/journal_probe.cc`, built with
Zephyr SDK 0.16.9 at `-Os`, occupies **2,250 code bytes**, with no static data/BSS.
Its undefined symbols are only `memcmp`, `memcpy` and `memset`; there is no
allocator dependency. The journal object is 260 bytes on ARM, including its device
reference; recovery cursor is at most 24 bytes; each allocation bitmap/plan is
32 bytes. These objects are caller-owned, so they do not appear in probe BSS.

GCC reports individual frames of 208 bytes for Append, 120 for Read and 24 for
the page writer, plus wrappers/callbacks. Frames nest: these figures are **not**
whole-thread stack peaks or the final keyboard RAM cost. Program/erase calls are
synchronous here; bounded CPU buffers do not imply bounded flash-lock latency.

```sh
python3 tools/tests/test_native_journal.py
CXX='clang++ -fsanitize=address,undefined -fno-omit-frame-pointer' \
  python3 tools/tests/test_native_journal.py
```

Tests are discovered by the existing native CI runner. Fake NOR validates
one-way programming and page splitting; it does not prove hardware program
restrictions, interrupted voltage behavior or electrical correctness.
Linux CI also runs this core under AddressSanitizer and UndefinedBehaviorSanitizer.
Locally, UndefinedBehaviorSanitizer passed. The combined sanitizer runtime on the
current Mac exits with SIGILL even for an empty program, so no local ASan pass is
claimed; the Linux check supplies that coverage.

## Next integration work

The native journal is not a complete store. Still required:

1. Bounded binary catalog, atomic publication and ownership validation, connected
   to native recovery and the allocator. No JSON catalog in firmware.
2. Bounded index construction/publication, tail reservation and explicit recovery
   when a surviving old catalog has more than 256 later edits.
3. Incremental cleaning/staging policies; the host whole-overlay compactor still
   needs too much temporary space for large near-full stores.
4. Diagnostic QSPI adapter, cancellable/background scheduling and measured lock
   hold times, RAM/stack and actual lookup performance, before routing translation
   or editor writes through the new store.

No reflashing is useful for this isolated core yet. A diagnostic adapter should
first exercise explicitly owned test data without erasing working dictionaries.
Firmware/installer replacement requires a fresh verified personal backup and
deliberate provisioning after the remaining gates pass.
