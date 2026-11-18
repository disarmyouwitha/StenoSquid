# Review: unified dictionary storage and benchmark spec

Reviewed 2026-11-17 against `main` at `4b31c60`. The spec under review is now tracked at
[docs/unified-dictionary-storage-spec.md](../docs/unified-dictionary-storage-spec.md).
The original review below describes that historical baseline; see the current
follow-up at the end before treating its implementation claims as current.
These are opinions plus one back-of-envelope measurement, not benchmark results.

## Summary

The process in the spec is good: benchmark first, label every target as a
hypothesis, fault-inject every transition, never erase old data to make room.
The scope is too large for one design, and Candidate A can be ruled out in an
hour of host-side arithmetic, not a benchmark project.

## Candidate A will almost certainly fail its own footprint gate

Estimated by packing the pinned source JSON into the current record format
(8-byte header + 4 bytes per stroke + text rounded to 4) and adding index
bytes at 9–16 per entry (two tables at 87%–50% load):

| Dictionary | Entries | Log records | With index |
|---|---:|---:|---:|
| Lapwing Base | 114,885 | 3.27 MB | 4.3–5.1 MB |
| Plover Base | 147,424 | 4.02 MB | 5.4–6.4 MB |
| Extras (4 files) | 29,094 | 0.88 MB | 1.1–1.4 MB |
| **Total** | **291,403** | **8.18 MB** | **10.8–12.8 MB** |

The same dictionaries compile to about **4.46 MB** today (1.83 + 2.18 + 0.45,
using a fresh Lapwing compile; the local `build/lapwing` file is stale).
So Candidate A costs roughly **6–8 MB more**, against the spec's "no more than
about 1 MiB extra" rule and "substantially over 2 MiB" cut-off for B.
On 16 MiB that leaves about 4–5 MB for personal data, well under today's
~190,000 entries. The gap comes from the compiled format sharing text across
dictionaries and storing about 17 bytes per entry, against about 37–44 in the log.

Recommendation: write the host estimator (spec §0.2) first, with this
calculation, and drop A without on-device latency work unless it surprises.

## What I would build instead (call it C)

Keep what is cheap and valuable in B; drop the expensive part.

1. **Catalog** (stable dictionary IDs, display name, enabled flag, priority)
   replacing the 8-bit selection mask. Small, versioned, CRC'd, stored in
   settings or one meta segment. Delivers enable/disable/reorder for all
   existing dictionaries and extends to named imports.
2. **Compiled bases stay in fixed, rebased slots** exactly as now. No
   relocatable extents, no extent allocator, no fragmentation or
   contiguous-allocation failure. The pinned Lapwing/Plover/Extras images are
   shipped artifacts, not user data, so they do not need a general allocator.
3. **Everything user-owned goes in the existing log**, with a dictionary ID
   added to the record. That covers the personal dictionary, named imports
   (`--import medical.json --as Medical`), and per-dictionary overlays
   (replacement and tombstone) for editing a compiled dictionary.
4. **Index keyed by outline only, not `(dictionary_id, outline)`.** One probe
   returns every record for that outline; the lookup then picks the highest
   enabled priority. A shared `(dict, outline)` key needs one probe per
   dictionary on every miss, and makes priority or enable changes touch
   lookups. With outline-only keys, reordering and toggling are catalog edits
   only, and a miss costs one probe however many dictionaries exist.
5. Overlay-first-then-compiled inside each dictionary, then global priority,
   as in the spec §4.4.

This gives users the features in the spec's "Done means" (independent select,
order, import, edit, backup) without the rebasing and relocation risk.

## Where the real latency risk is

The spec concentrates on p50/p99 lookup latency at 200 WPM. A lookup is a
handful of flash reads and is very unlikely to be the problem. The measured
risk is **stalls**: a 64 KiB erase or a cleaning/rebuild slice running while
holding the engine lock that the key path waits on forever. That is already
true on `main` today. Measure worst-case lock hold time (erase, cleaning
slice, rebuild slice, export) first. The cleanup branch with commit `998dde2`
("Keep key delivery responsive…") should be merged before any of this storage
work and its effect on the store's background work verified.

## Smaller points

- **Slot encoding:** today's slot is 10-bit tag + 8-bit segment + 14-bit offset
  and addresses at most 256 segments. More than 16 MiB means a wider slot,
  roughly doubling index size. Decide flash size first; it changes every
  number above. Treat 32/64 MiB as the separate hardware track the spec
  already says it is.
- **Cross-dictionary text sharing is lost** the moment a compiled collection
  is split or moved into the log; the spec notes this for splitting, but it is
  the biggest term above.
- **`--update` with user edits:** keep it simple. Overlays always win over a
  new upstream image; report overlay entries whose upstream word changed.
  Three-way merge machinery is not worth it for one maintainer.
- **Migration:** the "no format on open for legacy images" rule is right. The
  current `Open()` formats only when no valid meta and no valid record exist,
  which is the safe subset, but a catalog migration should refuse on anything
  else it does not recognise.
- **Manager UI (P5):** agree it is last. Nothing in P0–P4 needs it; the CLI
  and the existing Javelin browser tool cover those steps.
- **Sequencing:** P0 (measure) then a decision record is right. I would add a
  hard time-box and a question before it starts: which of these features does
  anyone actually need next (reorder, named imports, editing a base)? Each is
  separable.

## Suggested order

1. Merge cleanup phase-3 (key-thread responsiveness); measure lock hold times.
2. Host footprint estimator; confirm A is out (an hour).
3. Catalog + outline-keyed, dictionary-tagged log (C), fake-NOR tested like
   the current store, with an explicit migration from format v3.
4. Named imports and overlays on top.
5. Only then manager UI, and only if a larger flash is proven separately.


## Follow-up against current main — 2026-11-17

Reviewed against `169d1c9` (cleanup and Build-Up merge). This is a source review
and fresh size/count arithmetic, **not** the proposed latency/churn benchmark.

### Findings to resolve before implementation

1. **Do not treat the old locking description as current.** Cleanup is already
   merged. `flash_access.h` bounds system/key-thread acquisition to 10 ms and
   avoids waiting for wake there; `engine_work.c` moves initialization off that
   thread; `engine_stream.h` releases the lock between export chunks. Background
   maintenance can still hold it for a slow flash operation. Bounded waiting can
   discard an interrupted chord, so measure both worst-case lock hold time and
   dropped input: responsiveness alone does not pass the spec's zero-loss gate.

2. **Candidate C is a scope tradeoff, not a complete implementation of the spec.**
   Fixed base slots retain the base/personal capacity boundary and cannot borrow
   unused base capacity for imports. C may be the sensible simpler product, but
   choosing it requires explicitly relaxing the pooled-capacity goal. B remains
   the compact-format candidate that meets that goal. Do not quietly select C
   just because its visible dictionary actions resemble B's.

3. **An outline-only index needs a defined multimap, not the current unique-key
   table plus a dictionary field.** The current `Store::Find()` returns one live
   record. One initial probe does not automatically retrieve all dictionaries'
   entries for that outline. Define grouping/chains or collision scanning,
   duplicate replacement, per-dictionary tombstones, reverse-index filtering,
   power-failure recovery and GC before comparing its probe count to `(ID, key)`
   indexing. More than one flash read may be needed to resolve the winner.

4. **Unrecognized is not blank.** `Store::Open()` ignores invalid segment headers
   and can reach `Format()` when neither valid metadata nor recognized records
   exist. `Allocate()` then erases selected 64 KiB blocks. Therefore the original
   review's "safe subset" wording is insufficient for an unknown/new layout.
   Detect the layout before calling the old opener; unknown nonblank data must
   fail without writes. Fresh provisioning must be an explicit host operation.
   The owner wants no persistent migration reader or compatibility build.

5. **Larger flash requires a new encoding decision, not necessarily larger
   slots.** Current slots use 10 tag + 8 segment + 14 offset bits. A possible
   32-bit alternative spends one/two tag bits on 9/10-bit segment IDs, at the cost
   of more false tag matches and full-key checks. A wider slot or indirection is
   another choice. None establishes 32/64 MiB driver/XIP support; retain the
   separate addressing and hardware gates. "Roughly doubling index size" is not
   an inevitable consequence of higher capacity.

6. **Resolve the decision gate's order.** A host estimator can reject an
   infeasible footprint before investing in on-device latency work. The spec's
   "no selection until all results" should apply to selecting the surviving
   design, rather than requiring every failed candidate to get a device port.
   C's one-hour estimate is a proposed time-box, not evidence of actual effort.

### Verified current input sizes

The pinned images match `tools/tests/test_dictionary_contracts.py`:

| Image | Bytes | CRC32 |
| --- | ---: | --- |
| Lapwing Base | 1,830,444 | `00f2d636` |
| Plover Base | 2,339,620 | `3e8f8d88` |
| Grouped Extras | 469,812 | `98af1ecc` |
| **Total** | **4,639,876** | **4.640 MB / 4.425 MiB** |

The original 4.46 MB baseline and stale-Lapwing claim should not be used for
current capacity calculations. Counting the pinned source JSON gives 291,403
entries across the six named dictionaries: 114,885 Lapwing, 147,424 Plover,
146 Commands, 117 Numbers, 15,788 Proper Nouns and 13,043 UK Additions.

Applying only the current record-size formula gives **8,184,856 bytes** before
indexes, dictionary IDs, catalog, segment slack or staging. This supports the
concern that A is substantially larger, but is not a completed multi-dictionary
image measurement. Use explicitly labeled MB/MiB and actual allocated table
segments in the estimator; the current tables cap at 32 segments each, so the
half-full target is not maintained for every possible combined corpus.

The tail's present purpose is documented: `0xFF0000..0xFFEFFF` is 60 KiB reserved,
and `0xFFF000..0xFFFFFF` is the 4 KiB diagnostic scratch sector used by
`tools/qspi/src/scratch.c`. Keep it excluded until intentionally redesigned.

### Recommended next action

Build the host footprint estimator first, preserving dictionary identities and
using the pinned corpus. Compare A, B and the explicitly scoped fixed-slot C;
then choose whether pooled capacity is actually required. On-device benchmarks
should focus on worst-case maintenance interference as well as ordinary lookup
latency. A future manager does not itself require larger flash, and none of
these design notes implies that the proposed CLI commands already exist.
