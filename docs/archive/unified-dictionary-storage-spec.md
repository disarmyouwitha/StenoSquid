# StenoSquid Unified Dictionary Storage and Benchmark Specification

> Historical record. For current storage behavior and open validation, see [Native dictionary storage](../native-engine-integration.md).

**Status:** P3 opt-in catalog-driven engine/store and clean USB provisioning implemented; full native-profile CI and hardware acceptance pending\
**Repository baseline:** `disarmyouwitha/StenoSquid` `main` at `4b31c60` (2026-11-17)  
**Primary rule:** Benchmark first; choose the on-flash representation from measurements, not aesthetics.

## Current review context

The historical baseline above predates the cleanup merged at `169d1c9`.
Key delivery now has a bounded flash-lock wait, initialization runs on a worker,
and personal exports yield between chunks. Those changes reduce blocking risk;
they do not establish the zero-dropped-strokes performance gate below.
See the [review and current follow-up](unified-dictionary-storage-review.md).

**Owner constraint:** this is a single-user development project. Do not ship
migration readers, backward-compatibility shims or a separate migration build.
References below to migration/conversion mean an explicitly requested, backed-up
clean reprovisioning during development, not a permanent firmware feature.
Unknown nonblank storage must still be refused rather than automatically erased.

The proposed manager is StenoSquid-owned. Its features must not depend on changes
to the externally hosted Javelin Tools website. The subsequent [Phase-0 footprint decision](../dictionary-storage-decision.md)
rejects current-log-derived Candidate A and selects B for further development.
B still requires design and hardware validation. Candidate C would relax the
shared-pool goal.

## 1. Goal

Present every installed dictionary (personal, Lapwing Base, Plover Base, individual Lapwing Extras, and user imports) through one StenoSquid-owned interface. Users can independently enable, disable, prioritize, import, update, export, and ultimately edit dictionaries. Javelin remains the translation engine. Flash is managed as one shared capacity pool, with no permanent base-versus-personal quota.

Two storage candidates must be measured **before** committing to either:

- **A: Unified editable log.** All dictionaries use one multi-dictionary log, a shared record allocator, and shared forward/reverse indexes keyed by `(dictionary_id, outline)` and `(dictionary_id, text)`. All are directly editable.
- **B: Unified allocator, hybrid formats.** Compiled Javelin compact maps occupy relocatable *contiguous physical extents*. An editable log stores personal records and per-dictionary overlays (additions, replacements, tombstones). The manager presents the same editable interface for both types.

A and B must provide identical user-visible behavior. A choice of compact storage **does not** imply read-only bases in the UI.

## 2. Ground truth and constraints

Current `main` has fixed base images below `0x500000`, plus a 175-segment (64 KiB each) log at `0x500000..0xFEFFFF`. `0xFF0000..0xFFFFFF` remains reserved. The current log has a single logical personal dictionary, `uint8_t` segment identifiers and two flash-backed indexes. The compiled JSC4 maps use native pointers and a shared, deduplicated text block; the log stores text in individual records. The base loader expects a contiguous, correctly relocated image. These are *not* interchangeable formats.

**Hard constraints:**

1. The current nRF52840 code bounds QSPI reads/writes to the first 16 MiB; the current segment encoding can address at most 256 segments. **32/64 MiB support is a separate, mandatory hardware/driver/index-encoding gate**, not achieved by increasing `SEG_COUNT`.
2. Compiled JSC4 images need a known mapped load address at compile/relocation time; moving them without rebasing or fixing references is unsafe.
3. A flash write may clear bits but cannot restore them without sector erase. Every metadata transition, import, relocation and garbage-collection operation must survive interrupted power.
4. The current eight-bit selection mask cannot represent arbitrary imported dictionaries or reorder them. Replace it with a versioned catalog keyed by stable IDs.
5. Preserve the current firmware and on-flash layout as a recoverable baseline during development. Do not silently erase existing user data.

## 3. Phase 0: experiments and decision gate (must happen first)

### 0.1 Reproducible inputs

Use the **exact pinned source revisions** already fetched by `stenosquid.py`, `prepare_plover.py`, and `prepare_extras.py`; keep hashes, source paths, compiler version, Javelin revision, flash JEDEC ID, QSPI clock and firmware SHA in every report. Include Lapwing Base, Plover Base, every Extra individually, their complete enabled stack, and a representative personal dictionary. A synthetic or anonymized large dataset must also exercise roughly 50k, 100k, 190k and 200k entries where the format supports them. Never blend dictionaries into one map in a way that loses overlapping outlines: identity is `(dictionary_id, outline)`.

### 0.2 Host-side footprint measurement

Create `tools/bench/dictionary_footprint.py` and JSON/CSV output. Use the real `tools/dict_compiler/compile_dict.py` for compact JSC4 images. Use `tools/dictionaries/segment_image.py` as a **single-dictionary baseline only**; it is not yet a multi-dictionary format. Build a small, host-only **multi-dictionary log image/estimator** for Candidate A, including dictionary IDs, global forward/reverse index load factors, records, aligned lengths, catalog, metadata, 64 KiB segment slack, free segments needed for GC, and temporary space for updates.

For Candidate B measure both (i) existing grouped Extras images with shared deduplicated text and (ii) individually replaceable compact images; include extents, overlay log, catalog, fragmentation and update staging. The compiler deduplicates text *across dictionaries in a collection*, so splitting one collection can cost real space.

Report per dictionary and total: translation count, duplicate outlines across dictionaries, unique texts, compiled bytes, packed record bytes, index bytes, catalog bytes, allocated bytes, free bytes, update headroom and peak temporary bytes. Show a fresh install and a fragmented/edited state (10k edits, replacements and deletes, GC settled).

Sanity checks: compare byte-for-byte lookups/outputs against the JSON inputs, reject unsupported long entries explicitly, and account for the current log's roughly two half-full four-byte hash tables (which can use about **16 allocated index bytes per entry** before other overhead). **Do not assume the log is only 35% larger.**

### 0.3 On-device latency and power experiment

Create a benchmark mode that feeds the **same deterministic outline corpus** through the existing compiled-map lookup and current log lookup on the actual nRF52840, using the same flash speed (including the intended 2 MHz setting), firmware build and measurement method. Record forward hits, misses, hash collisions, one/two/multi-stroke keys, reverse lookups, dictionary priority fall-through, cold/warm access, and repeated words. Use a monotonic timer with wrap-safe sampling and low-overhead in-RAM trace buffers so instrumentation does not dominate timings.

Build a minimal Candidate-A prototype that tags keys with dictionary IDs **without shipping a new on-flash format**, or benchmark isolated one-dictionary lookups as a lower bound and clearly label the missing multi-dictionary overhead. For Candidate B benchmark Javelin compact lookups plus editable overlays, including tombstones and overrides. Measure Javelin's **entire translation path**, not only `Store::Find()`, to include dictionary-stack misses and priority resolution.

Collect median, p95, p99, maximum, total flash reads, CPU time, heap/stack/RAM, and read current during active lookup if measurement hardware is available. Replay real or representative stenography strokes at sustained **200 WPM** and faster **300–400 WPM bursts**, using captured inter-stroke intervals rather than treating 200 WPM as only 3.33 lookups/sec. Repeat with Bluetooth active, background GC/index rebuilds, after wake, and at near-full capacity. Assert exact output and **zero dropped/reordered strokes**. Measure wake-up separately from steady-state lookup latency.

### 0.4 Decision rules (provisional, revise only with documented evidence)

- **Prefer A (all log)** if the same dictionary corpus costs no more than about **1 MiB extra on the 16 MiB model**, fits with a healthy GC/update reserve, and shows no perceptible typing penalty. Proposed performance gate: no dropped/reordered strokes at 200 WPM or burst load; p99 end-to-end dictionary-path latency no more than 25% above existing compiled-base baseline, with a review of any reproducible lookup stalls over 10 ms. Evaluate *absolute* latencies too. These are engineering targets, not pre-existing measurements.
- **Discuss a tradeoff** if A costs 1–2 MiB more or shows modest but non-disruptive latency differences. Include data per dictionary and priority stack.
- **Prefer B (hybrid)** if A costs substantially over 2 MiB extra, materially harms latency, or cannot maintain safe GC/update headroom. B must still permit visible edits to compact dictionaries through overlays.
- **Selection gate:** a candidate may be rejected early when a conservative footprint bound already fails its target. Choosing the surviving design for production still requires footprint, realistic lookup, near-full churn, and 16 MiB capacity results. Do not infer 200,000-entry capacity from a 190,000-entry stress target alone.

Deliverable: `docs/dictionary-benchmark-report.md` plus machine-readable artifacts, reproducible commands, raw metrics and a signed-off decision record.

## 4. Shared on-flash model (both candidates)

### 4.1 Unified allocator and catalog

For the **16 MiB** model, plan around the external-flash data area `0x000000..0xFEFFFF` (255 possible 64 KiB segments), reserving `0xFF0000..0xFFFFFF` until its current purpose is confirmed. The figure is a candidate map, **not permission to erase old base images**. No permanently fixed base/personal partition. Every allocation is tracked in a versioned, CRC-protected catalog with crash-safe generation/commit records.

Each dictionary has a stable numeric ID, display name, source and pinned version/hash (if managed upstream), format, extent(s) or index root, logical priority, enabled state, editable/override policy, count, and integrity metadata. Separate identity from mutable name and display order. Keep multiple catalog generations and a bounded allocation/GC reserve. Define allocation ownership, active versus pending state, and recovery behavior before writing code.

### 4.2 Candidate A: one editable log

All records carry `dictionary_id` and outline/text. Forward and reverse indexes are **shared across dictionaries** and keyed by dictionary ID; do **not** create a minimum 64 KiB forward and reverse index pair for every tiny Extra. Add a catalog-backed dictionary-filtered iterator, independent export and delete-by-dictionary. Deletions use tombstones/commit-safe state transitions. GC moves live records, verifies them and advances durable references before retiring old sectors. Catalog edits and reorder operations do not rewrite translation records.

Current `user_dictionary_log.h` can inform the design, but changing the serialized key and indexes is a **new on-flash format** requiring versioning, conversion, property tests and corruption testing. Avoid a flag-only patch that leaves unqualified cross-dictionary collisions possible.

### 4.3 Candidate B: compact extent plus editable overlay

The same allocator provides contiguous, suitably aligned extents for JSC4 images. Build/relocate the image for its **actual mapped address**, verify image hashes and pointer bounds, then publish its catalog entry atomically. Edits to compact dictionaries append overlay records, including tombstones that suppress the underlying word. Lookup order inside each logical dictionary is **overlay first, compact image second**; the global dictionary priority then determines which dictionary wins. Editing an Extra is an edit to that named dictionary, not an invisible global personal override.

Replacing an image installs a staged image, checks it, atomically switches the catalog pointer and reclaims the old extent only after old readers finish. The allocator must handle fragmentation, contiguous-allocation failure and relocation safely. Full-image updates require enough temporary space for both versions, or must refuse without data loss and offer a documented host-backup/recovery flow. Do not silently wipe the old dictionary to make room.

### 4.4 Translation and ownership semantics

All dictionaries are individually enabled, ordered and displayed. Lookup winner: highest-priority **enabled** dictionary containing the outline; an overlay tombstone hides only that dictionary's underlying entry, allowing lower-priority dictionaries to win. Define an explicit policy for duplicate outlines in imported files and distinct dictionaries. Names can change; IDs must remain stable. A full dictionary delete should not delete someone else's dictionary. Bulk import, edit, export, rename, toggle and reorder must be safe to interrupt and retry.

## 5. Host commands and StenoSquid Manager

Preserve the current personal merge as an explicit compatible mode while introducing unambiguous operations (exact syntax can be finalized in the CLI design):

```sh
python3 stenosquid.py --install                       # missing pinned defaults
python3 stenosquid.py --import medical.json --as Medical # independent dictionary
python3 stenosquid.py --import user.json --merge-personal
python3 stenosquid.py --list-dictionaries
python3 stenosquid.py --update                        # installed dictionaries vs pinned manifest
python3 stenosquid.py --backup-all backup-directory
```

`--update` is *not* "download whatever is newest". It updates installed upstream-managed dictionaries to the manifest's tested/pinned revisions, preserving enable/priority settings and local edits. A future explicitly opted-in command could propose fresh upstream pins with a reviewable diff. For changed upstream entries and user edits, retain the user edit by default and provide a conflict report / reset-to-upstream action. Store origin and base-revision metadata so conflict detection is possible.

The separate StenoSquid Manager should use the existing HID transport where supported and expose: dictionary list/count/size/state, checkboxes, priority ordering, edit/add/remove with explicit target dictionary, import, update, backup/export, and Windows/Mac host-layout selection. Javelin stays responsible for translation. UI disconnect, HID timeout and app crash must never imply a committed operation that firmware has not acknowledged. Keep the manager UI a later phase after the storage/protocol is proven.

## 6. Migration and safety

1. **No in-place assumption:** old fixed bases and format-v3 personal log do not have the new unified catalog. Determine whether non-destructive staging genuinely fits before choosing migration strategy.
2. Export **every** old dictionary and user change to host files, verify content and checksums, and retain a recoverable external backup before any destructive rewrite.
3. Preflight physical flash size, layout version, free/staging space and pin-compatible firmware. If atomic in-place conversion is impossible, stop and require an explicit, recoverable host-assisted conversion; never erase old data on an optimistic estimate.
4. On first boot validate the catalog, allocated extents/indexes and record checksums, recover the last committed state, and quarantine corruption. No "format on open" for an unknown or legacy image.
5. Make firmware update and interrupted/abandoned import idempotent. Keep dictionary selection, order, host layout, and source provenance intact or migrate them explicitly.
6. Retain working external backup/restore. Onboard crash consistency does not protect against total flash failure or losing the keyboard.

## 7. Mandatory test matrix

**Pure host:** compiler/format parity; byte-accurate content, duplicate-outline isolation, priority/tombstone rules, Unicode and long text boundaries, 1–16 stroke outlines, empty dictionaries, many tiny Extras, catalog rename/order persistence, storage/capacity accounting, 16 MiB fragmentation, maximal index occupancy and allocation failures.

**Fake NOR / fault injection:** inject interruption or failed operation at every page program, header/commit-bit change, catalog update, erase, multi-segment import, index rebuild and GC relocation. After reboot, verify each dictionary reflects either the last committed old state or the fully committed new state; **never a partial blend or lost previously committed record**. Check all cross-references and free-list invariants. Test repeated resets during recovery and rollback after a corrupt catalog copy.

**Update/import:** fresh `--install`, second no-op install, one-Extra update, a one-word upstream correction, local override preserved, merge-personal compatibility, new named dictionary, delete/restore, near-full rejected update, interrupted replace and retry, truncated/corrupt JSON, malformed HID packets, wrong firmware/protocol version, disconnected client, and a dictionary exceeding physical capacity.

**Lookup behavior:** exact full-stack equivalence to current Javelin on pinned sources; precedence with duplicates; edit under a base; tombstone exposes lower priority; reverse lookup, stroke hints, browser export, Practice mode, PloverHID and standard typing unchanged. Test 200 WPM and burst playback while toggling priorities, GC running and Bluetooth traffic occurs; protect engine readers when a catalog/image version changes.

**Physical hardware:** at least one 16 MiB production-equivalent Winbond board. Run JEDEC/sector verification, flash power gating, idle/wake cycles, cold boots, USB/BLE, voltage interruption during import, catalog commit, erase and compaction, plus long-duration edit/wear tests. Host NOR simulations alone do not prove electrical behavior.

**Larger flash:** before advertising 32/64 MiB, individually validate exact manufacturer/part/footprint, 4-byte addressing, 16 MiB and 32 MiB boundary reads/writes, nRF QSPI memory-mapped limitations, absolute compact pointers, segment-ID width (255 usable on the proposed 16 MiB map versus 512/1024 total physical 64 KiB segments on 32/64 MiB), RAM/index sizes, passives and power characteristics. Do not infer compatibility from a same-capacity PMOD with different silicon.

## 8. Implementation phases and acceptance

- **P0: Measurement and decision (required):** pinned data and binaries, footprint report, prototype benchmark, real-board latency results, decision record. **Do not redesign production storage first.**
- **P1: Design freeze:** on-flash format, catalog schema, allocation/free/GC invariants, transaction protocol, compatibility/migration format and host commands; threat model for power failure and pointer lifetimes.
- **P2: Host prototype:** multi-dictionary allocator/catalog/lookup in fake NOR; property and fault-injection tests; import/export equivalence; conditional Candidate A or B implementation.
- **P3: Firmware integration:** segment allocation, engine adapters, runtime enable/priority, safe reader synchronization, CLI protocol, diagnostics and background work. Preserve existing builds for rollback.
- **P4: Migration, CLI and update:** tested backup, staged install/import, catalog upgrade, version/pin-aware `--update`, retry and destructive-space refusal.
- **P5: StenoSquid Manager:** independent browser UI using proven host protocol; host layout selection, dictionary actions, useful error messages, backup workflow.
- **P6: Hardware validation and release:** scripted cold/warm/200 WPM/GC tests, extensive power cuts, 16 MiB capacity boundary, long-duration wear, CI and release documentation. 32/64 MiB receive their own later signoff.

**Release blockers:** any lost committed translation under injected failures; incorrect precedence; an import/update that silently deletes unrelated data; measured typing stalls or dropped strokes; an unchecked pointer/extent move; inability to recover from a malformed catalog; or unsupported flash addressing claimed as working.

**Done means:** one StenoSquid-facing dictionary catalog and manager; every installed dictionary independently selectable and editable (directly or via transparent overlay); exact import/update/backup semantics; one pooled flash allocator; a benchmark-justified storage format; safe migration; and successful real-hardware validation.

## 9. References in current repository

- `src/steno_squid/user_dictionary_log.h` — current record/segment/index/GC implementation.
- `src/steno_squid/user_dictionary_store.cc` — QSPI adapter and Javelin personal-dictionary bridge.
- `src/steno_squid/lapwing_dictionary.cc` — native JSC4 compact loader and current read-only base adapters.
- `src/engine_init.cc` — hard-coded dictionary list and bit-mask selections.
- `tools/dict_compiler/compile_dict.py` — compact JSC4 compiler with cross-dictionary text deduplication.
- `tools/dictionaries/segment_image.py` — current *single-dictionary* log import image builder.
- `tools/tests/test_user_dictionary_log.cc` and `tools/tests/test_user_dictionary_import.cc` — fake NOR, capacity, import and fault-injection starting points.
- `.github/workflows/build.yml` — pinned dictionary compilation and current CI commands.

Current evidence: [Phase-0 measurements](../dictionary-benchmark-report.md),
[A/B decision](../dictionary-storage-decision.md),
[indexed host prototype](hybrid-index-publication.md), and
[bounded native core](hybrid-native-core.md), with
[first diagnostic QSPI hardware results](hybrid-hardware-probe.md).
[Native catalog/checkpoint and bridge progress](hybrid-native-catalog-checkpoint.md)

[Native live-edit recovery and snapshots](hybrid-native-live-store.md)

[Native firmware checkpoint transaction and current hardware test](hybrid-native-checkpoint-job.md)
includes a passed reboot-persistence diagnostic. The host models and isolated native
helpers do not complete the hardware/performance acceptance gates. P1 design
freeze and P2 large/near-full capacity work remain open while P3 native core
development starts; no production storage format has changed.

Current implementation: [native engine integration and acceptance plan](native-engine-development.md).
