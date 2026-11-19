# Candidate B: firmware checkpoint transaction job

Status: **native implementation, all firmware builds and sanitizer gates pass; hardware validation pending**.
Branch: `prototype/native-checkpoint-job`. Preserved checkpoint:
`checkpoint/native-live-recovery` at `7f5907c`. Everyday firmware still uses the
existing production dictionary store. This is development code, not a format
migration or a production activation.

## What is now native code

`tools/storage/native_checkpoint_job.h` replaces host-only checkpoint orchestration
with a fixed-memory C++ state machine. `native_merge_ops.h` provides the actual
journal-key/text comparisons, immutable index reads and streamed output callbacks.
The job itself allocates no heap memory and creates no thread.

The caller must hold an **exclusive maintenance lease for the whole job**, not
merely lock each individual `Step()`. Other allocation/catalog transactions must
wait. Reader snapshots may interleave; live append pauses. The caller can yield
between steps and use the job's pinned `Input()` for reads while live state is
being recovered after publication. That lease/scheduler is not installed in the
normal engine yet.

The job:

1. Pins the validated input, reads metadata incrementally, retains base/journal
   extents and records the old forward/reverse indexes.
2. Collects both catalogs and reader ownership, reserves worst-case capacity for
   **both** replacement indexes, and maintains the three-segment free reserve.
   Insufficient index or catalog-metadata space refuses before erase.
3. Erases one output segment per step, then streams the native merge through
   page-buffered, readback-verified sinks.
4. Validates output ordering, full record CRCs, active identities and latest-record
   correspondence. It verifies both directions of forward/reverse coverage, and
   checks every old/tail input key against the staged forward index.
5. Streams a replacement catalog, preserving names, flags, source hashes, priority
   and base/journal descriptors. Dropping old index descriptors remaps each
   dictionary's **descriptor index**; it does not move/relocate compiled base bytes.
6. Publishes last, incrementally recovers the new live state, then releases the
   maintenance pin and resumes append.

Empty output indexes are omitted. Set/remove/reset semantics are unchanged.
Old records belonging to deleted identities are filtered from the output; IDs
must never be reused. Catalog-driven dictionary deletion itself is still pending.

## Native journal rollover

`native_rollover_job.h` now handles the other common maintenance operation.
It pins the current view, inspects journal epochs, reserves one unowned segment,
initializes its header, appends the new journal descriptor to a streamed catalog,
publishes ownership and recovers the writer. It copies existing metadata unchanged
apart from the extent count. No edit is written into the new journal before its
ownership commit. Both jobs share the same whole-transaction lease requirement.

It supports a catalog with no journals, preserves an existing recent-edit tail,
and refuses epoch wrap or insufficient reserve before erase. Failed/cancelled
staging leaves only an unreferenced journal. Uncertain catalog publication requires
reopen, just as for checkpointing. This is rollover, not journal cleaning: old
journal extents remain protected until a future reclamation transaction.

The rollover suite passes 152 interrupted initialization/publication cases,
maximum-length text and old-reader isolation, cancellation/reuse, first-journal
creation, metadata copying, reserve refusal and epoch-wrap refusal. The first
31-record rollover takes 43 steps in its fixture.

## Cancellation and errors

Before acknowledged publication, cancellation abandons private staging and resumes
the old writer. Those unreferenced extents can be reserved and erased by a later
transaction; cancellation never erases a retained root or repairs metadata.
Reusing a cancelled job resets its sinks and builder.

After acknowledged publication, cancellation returns Busy: recovery must finish
against the new durable root. A catalog I/O/verification failure is treated as
an **uncertain commit**, even if `Published()` is false. Trust is invalidated,
queries/writes require reopen, and the next open determines the persistent result.
That flag means acknowledged publication, not proof that no commit occurred.

The job never automatically erases corrupt/unknown catalogs. Pinned catalog reuse
returns Busy. Cleanup, journal compaction, interrupted first provisioning and a
near-full reclamation policy remain separate work.

## Tests

`tools/tests/test_native_checkpoint_job.py` uses the actual job against strict NOR
and independent map/metadata checks:

- First checkpoint in 54 steps; repeated checkpoints, reset/remove, reader
  snapshots, pinned-slot backpressure and job reuse.
- Cancellation at 26 prepublication steps; refusal at eight postcommit steps.
- 220 interrupted erase/program cases, reconstructing fresh objects from flash.
- A committed catalog followed by failed readback: reopen recovers
  it, and the stale writer cannot retry.
- Staged index damage refuses publication and preserves the original catalogs.
- A full 4 KiB catalog refuses both maintenance operations before any erase or
  program, even when free data segments remain.
- 16-stroke outlines, UTF-8 and empty text, source hashes, reordered base
  descriptors, and all-reset output with no index extents.
- **20,000 records**, indexes spanning two 64 KiB segments each, replacement,
  tombstone, reset and addition: 160,101 bounded steps, followed by complete
  semantic index verification and reboot reconstruction.

The larger case measures correctness and work bounds, not device latency.
Individual comparisons can read up to a maximum-length translation. Catalog
publication/preflight still performs synchronous validation/erase/program work.
The whole job must not run under a typing-critical lock without scheduling and
contention measurements.

Local ARM compilation includes both real coordinators in Left-Diagnostic:
549,564 flash bytes and 239,958 linked RAM bytes. The checkpoint job is **5,056
bytes**, and rollover is **1,832 bytes**, of static ARM workspace (host ABI: 5,104
and 1,848). They must not be allocated on the HID stack.
The diagnostic carries both the old production store and experimental components.
Default firmware does not allocate this job. Runtime peaks remain unmeasured.

## Verified CI checkpoint

[Actions run 44379237087](https://github.com/disarmyouwitha/StenoSquid/actions/runs/44379237087)
passed all 12 firmware/probe variants, full native/display regressions, and Linux
ASan/UBSan at source `4f1a4b6`. Local UBSan also passes. The
[machine-readable report](prototypes/hybrid-native-checkpoint-job.json) records
checksums, workspaces, test counts and outstanding gates.

The final Boardsource Left-Diagnostic CI image links **549,656 flash bytes and
239,958 RAM bytes** (91.54% linked RAM). That includes the production engine,
logging and all experimental workspaces; runtime stack peaks remain pending.
The normal Boardsource Left-Default and Right-Default UF2s are **byte-identical**
to the previous live-recovery milestone, verified against run 44350590802.

Downloaded, UF2-family/address-checked artifacts:

- `build/native-jobs-ci-left/zmk.uf2`: Left-Diagnostic, SHA-256
  `d5a9dbca2fb0a4a3ba78d20838914dde3967146abd0050b7564bb6eb4205fbef`.
- `build/native-jobs-ci-right/zmk.uf2`: Right-Default, SHA-256
  `6734baad5ae72e828a2cd472b7225caa79a4d9f8382a5907baae1bcf6b473598`.

**Not flashed:** NICENANO was not mounted. Only the left needs updating for this
probe; the right artifact matches the installed right firmware. The local files
are ignored by git; CI retains the downloadable artifacts. Main is unchanged.

## Prepared hardware sequence

The diagnostic now uses **twelve reserved 4 KiB sectors at `0xFF4000..0xFFFFFF`**.
The extra sectors allow retaining two old catalog generations, staging both new
indexes and keeping three free segments. Physical operations remain 4 KiB in this
isolated test; the logical address stride remains 64 KiB. This is not validation
of full-size hardware extents or whole-store capacity.

`hold` refuses unless the entire twelve-sector test region is blank/readable.
Unknown nonblank bytes are never erased. This expanded fixture cannot be run with
the earlier seven-sector firmware. If it refuses, inspect the region rather than
forcing a cleanup.

The updated runner uses USB HID on the new Left-Diagnostic build:

```sh
python3 tools/bench/run_checkpoint_probe.py --output build/native-job/hardware.json
```

It backs up personal entries, checks production state, builds generation 2,
appends sequence 10, runs the **native checkpoint job** to generation 3/through 10,
then runs **native rollover** to generation 4 and writes sequence 11 in the new
journal. It checks pinned readers while each job steps, reboots normally, recovers
the checkpoint plus one-record tail and verifies cleanup/preservation. It then repeats the older checkpoint
fixture. A normal software reboot does not replace physical power-cut tests.

Only the left firmware changes. Keep the matching right firmware running and test
ordinary typing, onboard KAT and Practice/display afterward. No new hardware test
has been claimed yet.

## Next integration work

- Bounded overlong-tail rebuild and reclamation/cleaning, including reserve
  exhaustion. Both maintenance jobs currently retain all old journal extents.
- Compact-base loading and actual engine/editor/reverse-hint/export integration.
- Exclusive maintenance scheduling, cancellation and error reporting in firmware.
- Large-store hardware replay and typing contention, physical power interruption,
  runtime stack peaks and eventual clean provisioning tooling.

[Previous live recovery milestone](hybrid-native-live-store.md) remains the
preserved, CI-verified checkpoint.
