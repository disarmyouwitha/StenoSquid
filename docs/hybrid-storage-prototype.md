# Candidate B host reference model

Status: **catalog, pooled allocation and editable-overlay correctness prototype**.
It is Python running against an in-memory fake NOR device. It is not firmware,
not an approved production format and cannot open the keyboard or upload an
image. No migration reader, production-layout change or device erase is included.

## What is implemented

- One data pool of 64 KiB segments, with two catalog segments and a three-segment
  reserve. There are no fixed per-dictionary base/personal quotas.
- Stable numeric dictionary IDs, mutable names, enabled flags, explicit priority,
  source-content hashes and independently owned compact/overlay extents.
- Real JSC4 compact images compiled for the actual allocated mapped address.
  Host lookups and exports read those image bytes, not a shadow JSON base.
- Per-dictionary overlay additions, replacements and tombstones. A tombstone
  suppresses only its own base entry and allows lower-priority dictionaries to
  win. Clearing an edit restores that base entry. Upstream replacement retains
  the overlay. Empty imported dictionaries can acquire overlay entries later.
- Export of one logical dictionary and reverse lookup filtered by the same global
  precedence as forward lookup. Disabled dictionaries remain exportable.
- Copy-on-write catalog publication with CRC-protected headers/payloads and a
  final programmed commit word. Staged blobs are hashed and verified before the
  catalog is committed. The other committed generation remains recoverable.
- Old extents retained for both committed generations and explicit reader
  snapshots. Relocation recompiles compact pointers for the new extent.
- Allocation and catalog size preflight before any erase. Both insufficient
  space and lack of a contiguous extent refuse without erasing existing data.
- Read-only recovery from the newest valid catalog. Unknown nonblank flash is
  refused. A referenced-image failure can fall back to a predecessor and
  quarantine writes, preserving evidence rather than silently reclaiming it.

## Transaction and allocation rules

Only the host model's serialized writer may publish a transaction. A caller
holding `snapshot()` can keep reading that generation while later transactions
replace/delete its dictionaries. Allocations exclude active, retained and pinned
extents. A transaction plans all new extents and catalog payload before erasing
unreferenced space, programs/verifies blobs, erases the alternate catalog slot,
programs its header/payload and commits last. Old referenced blocks are not
rewritten. There is no automatic repair or formatting on open.

A block erase is modeled as sixteen 4 KiB observable steps. Page programs obey
256-byte boundaries and one-to-zero bit transitions. The fault injector can stop
before or partway through each program/erase step, including the commit word.
After interruption, recovery must expose exactly the previous or new committed
state. Repeated recovery performs no writes.

This is a single-process reference, not proof of real RTOS concurrency. Firmware
must enforce equivalent ownership, reader lifetime and memory-ordering rules.

## Commands and evidence

```sh
python3 tools/tests/test_hybrid_storage.py
python3 tools/storage/exercise_hybrid.py --operations 10000
python3 tools/bench/dictionary_footprint.py
python3 tools/storage/exercise_pinned_hybrid.py
```

The first test is automatically discovered by `tools/tests/run_all.py` and CI.
The 10,000-edit and full pinned-corpus exercises are explicit development runs;
they write reports under ignored `build/hybrid/`. Published summaries live in
[docs/prototypes](prototypes/). Source dictionaries are hash-checked against the
same manifest as production setup.

Tests cover priority, dictionary identity, Unicode overlays, tombstone fall-through,
reverse lookup, exports, enabled state, rename/reorder/delete, upstream replacement,
reader pinning, rebasing, reboot, fragmentation refusal and corruption quarantine.
Small-fixture fault injection covers add, edit, tombstone, image update, relocation,
reorder and deletion. It tests four torn-prefix lengths at every modeled operation;
this is not exhaustive testing of every byte value or every possible full image.

## Results for this reference revision

- **928** torn-operation cut points passed across seven operation types. Recovery
  exposed exactly the old or new committed state, and repeated reboots wrote
  nothing. [Fault-test summary](prototypes/hybrid-fault-tests.json).
- **10,000** deterministic edits/clears/tombstones, with periodic priority changes
  and reboots, passed an independent expected-lookup model. This tiny-corpus run
  programmed **11,752,040 bytes** and executed **324,416 modeled 4 KiB erase
  steps**. That deliberately high write amplification rules out shipping the
  snapshot strategy unchanged. [Churn report](prototypes/hybrid-churn.json).
- The full **291,403-entry** pinned corpus was compiled into six separately
  allocated/rebased compact images, exported from those actual bytes and checked
  against canonicalized source outlines and UTF-8 translations. A full Lapwing
  replacement retained its local overlay and the old reader's extent. Reopening
  the catalog preserved the result. [Pinned-corpus report](prototypes/hybrid-pinned.json).
- The final pinned experiment had **77 active segments** (5,046,272 bytes,
  including catalog and one overlay snapshot), with up to **105 observed occupied
  segments** while retaining generations/readers during the experiment. These
  are host-model observations, not a guarantee about arbitrary fragmentation or
  future overlay/index overhead. Largest-Plover staging and near-full full-corpus
  workloads remain additional tests.

All reports include the model source hash. No hardware state was touched.

## Deliberate limits before production design freeze

1. **Overlay writes are whole snapshots.** This makes the reference easy to
   validate but rewrites a full small overlay and catalog on every edit. It is
   deliberately unsuitable for production wear or latency. The next design step
   is a shared append-only overlay journal/index with bounded maintenance, checked
   against this model as a semantic oracle. There are no production index/RAM
   figures yet.
2. Catalogs are canonical JSON in two 64 KiB slots. Production should use a compact
   bounded representation and append commits rather than erasing a slot per edit.
   Source hashes here describe imported content, not an upstream update/conflict
   protocol. The catalog and transaction API are internal model calls, not CLI/HID
   commands promised to users.
3. Unreferenced extents are erased lazily on reuse. There is no live-segment GC,
   automatic defragmenter or guarantee that a contiguous update fits despite
   enough total free space. Refusing safely is implemented; solving fragmentation
   still needs design and measurement.
4. Current compiled-image enumeration is one dictionary per extent. Grouped images,
   shared text across independently replaceable dictionaries and their ownership
   rules remain open. No concurrent multi-image import/update protocol exists yet.
5. The host has ample RAM and can hash/read entire blobs. These are not firmware
   RAM, timing, power or XIP measurements. Host string lengths do not establish
   the device loader's translation-buffer capability.
6. No physical power cuts, near-full production-size churn, controlled stroke
   playback, flash timing changes or 32/64 MiB addressing support are claimed.

Candidate B remains the development direction. This reference establishes testable
semantics and recovery invariants; it does not freeze a production flash format.
