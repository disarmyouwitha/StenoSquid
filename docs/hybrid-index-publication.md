# Candidate B: index publication and reverse lookup

Status: **integrated host model and native reader building blocks**, not installed
firmware. Production storage and the installer are unchanged; no migrations.
This follows the [binary/index experiment](hybrid-binary-index.md).

## Publication and readers

`tools/storage/indexed_journal.py` connects binary records to two immutable
uint32-address tables in fake NOR. Forward order is dictionary ID plus the complete
outline. Reverse order is UTF-8 translation bytes, ID, then outline. The tables
reference the latest committed set/tombstone for each key; reset exposes the base
and needs no checkpoint entry. Reverse indexes contain sets only. Deleted IDs
can remain in an old checkpoint but cannot produce results or be reused.

The model plans record/index extents, staging reserve and catalog size before
erasing. It stages both tables, verifies their hashes, sorting, complete coverage
and record identity, then publishes them together through the alternate catalog's
final commit word. A checkpoint carries the sequence boundary it covers. Old
catalogs and pinned readers protect their index and record extents from reuse.

New edits use a fixed recent-address tail. At 256 edits, checkpointing happens
before another edit is committed. Invalid requests are rejected before that
checkpoint can write. Forward queries check recent addresses newest first, then
binary-search the checkpoint. They do not replay the full journal into a lookup
map. Python open/write/build paths still replay records and use unrestricted
heaps, sorting and JSON catalogs; **they are not a firmware-sized builder**.

Compaction stages current live records, builds both indexes against the new
addresses, verifies them, and publishes one catalog. Pinned readers retain their
old records and sequence boundary. This whole-live-overlay reference compactor
is not an incremental device cleaner.

## Reverse cursors

`tools/storage/reverse_cursor.h` first seeks to a translation, then examines at
most the requested candidate budget per page (1–256). Stale/shadowed candidates
count against that budget even when they produce no output. The caller filters
effective edit address, enabled dictionaries and the global priority winner.
There is no cap on the total number of matching outlines.

The cursor is at most twelve bytes. Its owner must pin the catalog, table and
records for its lifetime; the native helper does not manage ownership itself.
After an error, discard that page's partial output: the cursor position remains
unchanged for retry. Exact UTF-8 text comparison uses a 32-byte chunk buffer,
including at the 2,047-byte translation limit.

The host generator owns its reader pin, merges checkpoint/recent/base candidates,
filters overridden records, and prevents duplicate overlay/base results. Closing
or exhausting it releases the pin. Empty pages may mean more work remains. Host
base enumeration is a correctness oracle; native compiled-base reverse cursors
and scheduling have not been integrated or timed.

## Recovery checks

Opening validates hashes, extent bounds/ownership, table order and coverage. A
duplicate/omitted address is rejected even if its hash is updated. Recovery and
reverse reads perform no repair writes. Unknown nonblank storage is refused;
committed corruption causes quarantine/fallback or refusal. If only an older
catalog survives and its accumulated tail exceeds 256 records, opening refuses
rather than dropping edits or using an unbounded lookup. Explicit bounded rebuild
for that corruption case remains future work.

The indexed test passed four torn-prefix lengths at every modeled program/erase
operation for these representative transactions:

| Transaction | Cases |
| --- | ---: |
| First checkpoint | 224 |
| Compact records and publish both indexes | 316 |
| Replace a checkpoint | 224 |
| Automatic checkpoint before edit 257 | 236 |
| Total | 1,000 |

Each trial reopens to old/completed logical contents, checks recovery made no
writes, makes a further edit, and reopens again. Tests also cover reserve refusal
before writes, metadata/base changes, deleted IDs, resets, tombstones and an
independent replay oracle over 320 mixed edits. Sixty-five outlines sharing one
translation retain a stable paginated view across edits, disabling a dictionary,
and compaction, without duplicates.
The [reproducible report](prototypes/hybrid-indexed-journal.json) includes source
hashes and observed fault-case counts.

The native test separately visits **200,000 equal-translation candidates**, seven
per page, filters half, and emits all 100,000 expected results. It checks cursor
errors and mid-page retry, plus empty/prefix text and UTF-8 crossing a chunk
boundary at maximum length.

The expanded Cortex-M4 probe (Zephyr SDK 0.16.9, `-Os`) compiles forward search,
record/text comparison and reverse seek/page helpers into **1,302 code bytes**,
with no static data/BSS or unresolved allocator symbols. The 1,028-byte recent
tail remains caller-owned. GCC reports individual frames of 72 bytes for text
comparison, 16 for its wrapper, 40 each for reverse seek/page wrappers and 16 for
their address reader. These are not whole-thread peaks or full firmware costs.

```sh
python3 tools/tests/test_indexed_journal.py
python3 tools/tests/test_packed_index.py
# Optional machine-readable evidence:
python3 tools/tests/test_indexed_journal.py --report build/index-publication.json
```

## Remaining gates

- Bounded native catalog/allocator, writer, checkpoint builder and recovery.
- Incremental cleaning/checkpoint policy with measured staging space and write
  amplification. Whole-overlay compaction can refuse before physical space is
  exhausted; this model does not establish a 200,000-entry device capacity.
- Read-only QSPI timing of full tails, long text and reverse filtering while
  typing; compiled-base reverse integration and bounded pin/cursor ownership.
- Full firmware RAM/flash/stack checks, installer/editor integration and deliberate
  backed-up provisioning after those gates pass.

Forward and reverse tables together use up to eight bytes per live set, or
1.6 MB at 200,000 entries, **before** segment rounding, recovery copies, staging
and record storage. Tombstones need only forward entries. Bases retain their
existing compact indexes. No hardware latency, wear or battery claim is made.
