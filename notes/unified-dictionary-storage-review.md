# Review: unified dictionary storage and benchmark spec

Reviewed 2026-11-17 against `main` at `4b31c60`. The spec under review is
`stenosquid-unified-dictionary-storage-spec.md` (kept outside the repo).
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
