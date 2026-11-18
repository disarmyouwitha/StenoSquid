# Candidate B: native live-edit recovery and snapshots

Status: **implemented and host-tested; extended hardware probe ready to build**.
Development branch: `prototype/native-live-store`. Preserved hardware checkpoint:
`checkpoint/native-catalog-reboot` (`5af818f`). Neither branch changes the everyday
engine's production format-3 dictionary store. No migrations are introduced.

## What this adds

`tools/storage/native_live_journal.h` attaches to a validated catalog and rebuilds
live state one dictionary row, extent descriptor or committed record per step.
It remembers at most 256 records newer than the checkpoint, with 64 dictionary
identities and a 32-byte journal ownership bitmap. It verifies epoch/sequence
ordering and the checkpoint boundary. Recovery never erases or formats storage.

Live append uses the existing commit-last journal writer. It publishes an edit to
RAM only after verified commit. An uncertain write/readback result stops writes
and queries until recovery resolves it. A full tail requests checkpointing; a full
or torn journal tail requests a new journal. More than 256 recovered uncheckpointed
records requests a rebuild rather than silently dropping records. That larger
rebuild is not implemented yet.

Snapshots copy the small recent-edit tail and acquire a catalog pin. They retain
old values while later edits become visible to live readers. A maintenance snapshot
pauses append while tables are staged, but permits existing snapshot reads. The
caller must serialize live operations, metadata changes and publication. These
classes do not introduce their own worker, lock, scheduler or erase policy.

`native_ownership.h` collects both retained catalogs and reader pins before an
allocation plan reserves output extents. Any uncertainty returns an all-occupied
map. Staging must reserve **both** replacement tables before erasing either. The
integrated test fixture exercises that sequence and a journal rollover which
publishes ownership before the new segment accepts records. The fixture uses STL
only as host test machinery; it is not a firmware transaction coordinator.

## Lookup cost and trust

Catalog pins now use roots already validated by open/publication, with zero flash
reads. Explicit invalidation revokes that trust before out-of-band cleanup. A
failed publication cannot leave a stale root writable.

A lookup compares record headers/keys before reading translation text. Full
payload CRC/UTF-8 validation occurs at append/recovery and again on an exact
match. With 256 distinct maximum-length translations, a missing single-stroke
query reads **14,336 bytes**, instead of scanning all 524,032 text bytes as well.
This is a host read-volume measurement, **not** measured device latency.

This relies on immutable owned records after validation. A key bit flip after
recovery can cause a false miss until scrub/reopen; this path is not a substitute
for full integrity validation. Index coverage and journal ownership must be
validated by the store's root validator before trusting a catalog. The future
production coordinator must define scrub/error handling and retention policy.

## Verification

The ordinary native runner discovers the new test automatically. The Linux CI
sanitizer gate explicitly includes it as well.

- 700 seeded mixed set/remove/reset edits, 22,400 independent oracle lookups,
  incremental recovery and checkpoint replacement.
- Snapshot isolation across append/publication; pinned catalog reuse returns Busy;
  released/cancelled tokens cannot authorize reads.
- 16 torn append cases, a committed write with failed readback, sealed-tail
  rollover, maximum-length translations, full-tail and near-full safe refusal.
- Recovery refuses an overlong 257-record tail. Matched payload corruption stops
  live queries. Ownership failure leaves no allocatable segments.
- Full host suite and local UBSan pass. Existing native journal/catalog/transaction
  fault-injection suites remain enabled; no format change to their wire records.

The local Boardsource Left-Diagnostic ARM build uses **537,644 bytes internal
flash and 233,070 bytes linked RAM**. The live state object is 1,728 bytes; a reader
snapshot is 1,376 bytes; the catalog object is 392 bytes. Keep snapshots off the
small HID stack. These are static/compiler measurements, not runtime peaks.
The probe is diagnostic-only; this does not add those objects to Left-Default.

## Hardware test prepared for the next flash

The existing seven-sector probe now accepts `live` and `live_check`:

1. `hold` builds generation 2 with checkpoint sequence 9.
2. `live` appends sequence 10 and checks live `live` versus the old snapshot's
   `newer` value.
3. `reboot` acknowledges and schedules a normal software reboot.
4. `live_check` reconstructs the one-record tail using persistent bytes.
5. `clean` verifies the known fixture before erasing its seven reserved sectors.

Run the automated sequence, production-preservation checks and private backup with:

```sh
python3 tools/bench/run_checkpoint_probe.py --output build/native-live/hardware.json
```

It requires the new Left-Diagnostic firmware over USB HID, no serial port. Close
other editor connections and do not type during the synchronous diagnostic. It
uses only `0xFF9000..0xFFFFFF`; the virtual segments map to seven physical 4 KiB
sectors. It does not establish full 64 KiB erase behavior, whole-store capacity,
power-cut safety on the device or zero dropped strokes during maintenance.

The extended probe has **not** been run on hardware yet. The installed checkpoint
firmware predates its two new commands. Use the new branch's Actions artifact
for the next flash, then test ordinary typing, onboard KAT and Practice/display.

## Remaining before production integration

- Native transaction coordinator and bounded rebuild/cleaning policy, including
  interrupted staging, cancellation and reserve exhaustion.
- Catalog-driven compact-base loader/relocation and live adapter installation.
- Editor/reverse hints/export/provisioning wired to the new store.
- Large-store and maintenance-contention replay with actual typing; runtime stack
  peaks and physical power interruption tests.

The prior [catalog/checkpoint milestone](hybrid-native-catalog-checkpoint.md) remains
valid evidence for the installed build. Host and compiler results above do not
replace the outstanding hardware gates.
