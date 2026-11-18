# Candidate B: firmware checkpoint transaction job

Status: **native implementation and targeted host tests pass; hardware validation pending**.
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
   Insufficient space refuses before erase.
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

Local ARM compilation includes this real coordinator in Left-Diagnostic:
545,700 flash bytes and 238,126 linked RAM bytes. The job is **5,056 bytes** of
static ARM workspace (host ABI: 5,104); it must not be allocated on the HID stack.
The diagnostic carries both the old production store and experimental components.
Default firmware does not allocate this job. Runtime peaks remain unmeasured.

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
checks a pinned reader while the job steps, reboots normally, recovers the zero-tail
checkpoint and verifies cleanup/preservation. It then repeats the older checkpoint
fixture. A normal software reboot does not replace physical power-cut tests.

Only the left firmware changes. Keep the matching right firmware running and test
ordinary typing, onboard KAT and Practice/display afterward. No new hardware test
has been claimed yet.

## Next integration work

- Native journal rollover/publication job, bounded overlong-tail rebuild and
  reclamation/cleaning, including reserve exhaustion.
- Compact-base loading and actual engine/editor/reverse-hint/export integration.
- Exclusive maintenance scheduling, cancellation and error reporting in firmware.
- Large-store hardware replay and typing contention, physical power interruption,
  runtime stack peaks and eventual clean provisioning tooling.

[Previous live recovery milestone](hybrid-native-live-store.md) remains the
preserved, CI-verified checkpoint.
