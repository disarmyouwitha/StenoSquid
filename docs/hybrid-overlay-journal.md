# Shared overlay journal reference

Status: **host-only append/compaction prototype; no firmware integration**.
This extends the [B reference model](hybrid-storage-prototype.md), without changing
its snapshot implementation or adding migration readers. The two host models are
separate development formats. Neither can open a hardware device.

## Edit path

All logical dictionaries share journal segments from the same allocator as compact
images. Each record carries a stable dictionary ID, outline, operation and UTF-8
text. `set` adds/replaces, `remove` hides only that dictionary's compact entry,
and `reset` clears a local edit to expose its base again. Priority and enabled
state remain catalog properties. A base update keeps its journal edits.

A record has a 32-byte header containing length, sequence and header/payload
checksums. It occupies whole 256-byte pages. The payload is programmed before a
separate final commit word. Ordinary edits append in the existing segment:
**no catalog rewrite and no segment erase per edit**. An empty initialized segment
is published in the catalog only when a new segment is needed. Metadata changes
still use the catalog publication mechanism from the reference model.

The host model validates and replays committed records on open. An incomplete
record seals the tail: retry allocates another segment rather than attempting
to program over the torn pages. A corrupt committed record causes refusal or
read-only fallback/quarantine, never silent truncation. Repeated recovery performs
no writes. Unknown nonblank storage is refused.

## Readers and compaction

Reader snapshots pin both their extents and the committed sequence they saw.
Later edits appended to a shared segment cannot change that reader's view.
Compaction creates new journal segments containing the latest set/tombstone for
each live dictionary/outline; reset and deleted-dictionary records disappear.
It validates the staged segments, then publishes the new root once. Old segments
remain protected by retained catalog generations and reader pins. Unreferenced
segments are erased only when reused.

Staging and reserve checks happen before erasing. If compaction or segment growth
cannot fit safely, the operation refuses while preserving the existing lookup
state. There is no automatic defragmenter, live-segment cleaning scheduler or
firmware-sized index in this reference.

## Checks and commands

```sh
python3 tools/tests/test_hybrid_journal.py
python3 tools/storage/exercise_hybrid.py --journal \
  --operations 10000 --output build/journal/churn.json
```

The native runner/CI discovers the journal test automatically. It covers append,
new-segment publication, reset, tombstone, compaction, multi-page entries and
catalog reorder/delete with existing journal entries. At each modeled page
program/erase step it tests four torn-prefix lengths and recovers to old/new
logical contents. Tests also exercise post-recovery edits, pinned-reader isolation,
unknown/corrupt data, a sealed tail, priority/reverse semantics and reserve refusal.
This is representative operation fault injection, not exhaustive physical flash
fault coverage.

The 10,000-edit experiment uses the same seed, dictionary contents, edits, reorder
cadence and reboot cadence as the snapshot reference. Journal compaction runs every
256 input edits. Both models check every operation against the same independent
expected-lookup model. The byte/erase counts are fake-NOR observations, not
measured battery, device wear or execution time. Host runtime is dominated by
replaying JSON records without a firmware index and must not be compared as a
keyboard latency result.

## Before reflash

No reflash is required yet. Firmware integration still needs:

- A compact binary overlay record and bounded forward/reverse index design,
  rather than JSON replay and an unrestricted host heap.
- Bounded journal cleaning/rebuild work, safe index publication and low-wear
  catalog commits; near-full and pinned-reader stress tests.
- Full-corpus overlay and largest-image staging tests, then device resource builds.
- Diagnostic stage timing separating lookup, hints, output and waiting; exact
  controlled stroke playback during edits/compaction.

Only after those pieces build and pass recovery/resource checks should a matching
left/right diagnostic pair be flashed for hardware validation. No production
storage layout has been selected or written to the keyboard here.
