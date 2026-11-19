# Candidate B: full engine/store integration test build

Status: **Native is the everyday dictionary store for both board targets.**
The earlier opt-in checkpoints below record development builds and board results.

## What runs on the keyboard

`CONFIG_ZMK_STENO_SQUID_NATIVE_STORE=y` installs the native store into the actual
Javelin dictionary collection. It replaces the old fixed-base loader and personal
store for this build; it does not read, convert or migrate their storage format.

- Both catalogs and all dictionaries use the shared 16 MiB allocation model.
- Catalog order and enabled flags construct Javelin's collection at startup.
  Existing browser checkboxes save directly to the catalog before acknowledging.
- Six compact JSC4 base images load from catalog extents at their compiled XIP
  addresses, with per-dictionary native set/remove/reset overlays.
- Existing personal add/replace/remove, export, reverse lookup, prefix/partial
  outline queries and reset commands use the native store. Personal reset commits
  a fresh, never-reused identity atomically; it does not append hundreds of deletes.
- All set results, including compact-base text, become owned RAM strings before
  their pin is released. Base export uses erratum-safe QSPI accessors rather than
  the upstream generic exporter's byte loads.
- A per-dictionary outline-length bitset is rebuilt from validated journals and
  retained with reader snapshots. Empty or absent-length overlay misses need zero
  flash reads. Resident wrappers advertise the actual base/overlay maximum,
  avoiding pointless 16-stroke history searches for an empty personal dictionary.
  No per-edit metadata save or on-flash format change is needed.
- Reverse overlays use the ordered reverse index and bounded recent tail. Existing
  single-stroke SHI1 base hints are filtered against overlays and winning priority.
- Checkpoint and rollover jobs run in slices on the existing engine work queue.
  Each slice ends at 2 ms, 128 steps, or the arrival of buffered input; blocking
  flash operations finish before yielding. Work is rescheduled after 2 ms instead
  of adding an 8 ms pause to every small metadata/index operation.
  Append pauses during a job; lookup pins its immutable input while recovery runs.
  Editors get Busy and retry rather than receiving a false success.
- Native-only input buffering retains up to 512 physical press/release events
  during flash maintenance, preserving their order and original timestamps.
  Mode changes cancel pending chords. Overflow cancels the whole pending batch
  and waits for all physical keys up, rather than translating a partial chord.
  `native_manage.py --status` reports queue high-water and overflow counters.
  Speed deadlines use captured event times; timer work waits for pending replay.
- Read sessions keep the flash awake during maintenance and export. Normal idle
  shutdown/deep sleep retain the existing power driver and pin configuration.

Checkpoint publication now detaches journals with no surviving indexed entries.
It retains every live set/tombstone, the latest committed sequence's journal and
the append journal (including an empty one). This phase changes ownership metadata;
it does not erase the old journals. The alternate catalog and reader leases keep
them protected until later publication/release. Only then can ordinary staging
reuse those segments. Journals containing any live entry still require a future
relocation cleaner; this is deliberately a narrower reclamation step.

A torn partner catalog no longer hides a fully validated surviving catalog.
Native open exposes it **read-only** for translation/export, reports `read_only`
in status, and refuses edits, checkbox writes, reset and maintenance without
changing flash. It does not automatically erase damaged metadata. Export a backup
through Javelin Tools before choosing explicit clean reprovisioning.

The native base-layout fingerprint has its own settings key. Unchanged bases can
skip another full CRC pass on wake; mapped native lookups still exercise the base
and generic startup validates catalog/journal/index integrity. Explicit install
invalidates that trust **before** any erase and refuses if invalidation fails.
A trusted cold boot schedules opportunistic base scrubbing after a minute; a full
CRC starts the daily interval. Scrubbing neither powers nor holds the rail on.

The first build reserves 16 resident dictionary wrappers (configurable up to the
catalog's 64-dictionary bound, with a corresponding RAM cost). Base role IDs are
stable; the hosted editor's personal dictionary keeps its `user_dictionary` name.

## Clean shared-pool image

`tools/storage/build_native_image.py` compiles each pinned JSON source as its own
relocated compact image, with a source SHA-256 and content CRC in the catalog.
It creates both committed catalog copies and one empty journal. No old personal
copies or legacy readers are retained in the native pool.

| Region | Initial contents |
| --- | --- |
| Segments 0–1 | Two native catalog generations |
| Segment 2 | Empty journal, epoch 1 |
| Segment 3 onward | Six independently owned compact base extents |
| Remaining pool through segment 254 | Shared free capacity; staged indexes and journals allocate here |
| Segment 255 (`0xFF0000..0xFFFFFF`) | Reserved; installer does not erase it |

The pinned build initially leaves **178 free 64 KiB segments** (11,665,408 bytes).
This is free pool space, not an entry-capacity promise: retained index versions,
output staging and the three-segment reserve also consume it as the store grows.

Factory priority and IDs:

| ID | Dictionary | Default |
| ---: | --- | --- |
| 1 | user_dictionary | Enabled |
| 2 | Lapwing Commands | Enabled |
| 3 | Lapwing Numbers | Enabled |
| 4 | Lapwing UK Additions | Disabled |
| 5 | Lapwing Proper Nouns | Enabled |
| 6 | Lapwing Base | Enabled |
| 7 | Plover Base | Disabled |

After personal reset its ID changes; the USB manager reads the current ID instead
of assuming 1. Other IDs are never repurposed.

## Firmware and install

The `Left-Default` and `Left-No-Display` firmware use Native on both board targets.
An existing Native store stays in place across the firmware update. An older
fixed-layout store needs a personal backup and clean reprovision.

Leave the left running over USB and close other editor connections:

```sh
python3 stenosquid.py --status
python3 stenosquid.py --install --confirm-erase
```

The explicit confirmation flag replaces **all external dictionaries in the 255
pool segments**. It leaves the reserved final segment, internal ZMK settings,
Bluetooth pairing, Studio keymap, host profile and Practice progress untouched.
The tool builds the pinned image if none is supplied; it sends bounded USB HID
commands, verifies the full image, and waits for the normal reboot and READY.
Pop displays Install. No serial port, polling daemon or second provisioning
firmware is required. A stopped transfer releases its power hold after a minute;
partial storage remains unavailable until an explicit retry.

Returning to an older fixed-layout release requires an external personal backup,
the matching older firmware and tools, and fresh fixed-layout provisioning.

A private pre-test personal backup was captured locally at
`build/native-engine/owner-personal-backup.json` (zero entries on the connected
keyboard). It is not checked in.

## Packed Lapwing and backup restore

The proposed fresh install map groups Lapwing Base, Commands, Numbers, UK
Additions and Proper Nouns in one JSC4 collection. Each keeps its catalog ID,
order, enabled flag, content hash and hint index. Catalog row byte 10 selects
the member within the collection. The flash verifier checks the collection
once, while the catalog rejects duplicate member references.
See the [External Memory Map](../assets/qspi-flash-map-native.png).

On a board using the previous layout, back up personal entries **before**
flashing the new Native firmware. Use the same backup for a clean reprovision
after flashing. These commands operate on the selected left half over USB.

```sh
python3 stenosquid.py --backup personal.json
# Flash the matching Native UF2.
python3 stenosquid.py --install --confirm-erase --restore personal.json
python3 stenosquid.py --status
```

The restore validates the entire JSON and capacity before erasing. The host
writes the journal records and sorts the forward and reverse indexes once.
The installer writes referenced data before either catalog. A strict NOR
host run reopened and checked all 147,424 Plover entries restored as personal
entries, leaving 29 segments free.
This path provisions a complete image. A power cut during provisioning can
leave the board unprovisioned. Keep the backup and rerun the install.
The commands below cover in-place bulk merge and explicit live-record compaction.

On the new firmware, an explicit full verification shows **Verify** on the
right display and cancels steno chords entered during the scan. Regular
checkpoint delivery still replays queued keys in order.

On Boardsource hardware, the packed build from Actions run `44463275181`
passed full install CRC verification and rebooted READY. The five Lapwing
checkboxes each changed independently, then the previous selections were
restored. A separate normal reboot retained those selections and all three
personal entries. KAT and HE/HROE returned their expected translations.
Re-running the normal install command reported the store already installed.
The explicit integrity scan passed in 8.157 seconds. The queue logged a
high-water mark of 42 events and zero overflows without increasing the replay
delay. The owner confirmed the editor stayed blank during and after Verify,
and KAT typed cat normally after the scan.

## In-place bulk merge and live-record compaction

On the staged-update build, `python3 stenosquid.py --import personal.json`
uses staged USB HID import for 256 or more entries. Smaller imports use the
ordinary entry commands. `python3 stenosquid.py --compact` runs a reclamation
pass. Both commands operate on Native and use the existing board layout.

The host reads the trusted checkpoint and its journal tail, then plans the
complete change against the union of both catalog copies and active reader
ownership. It writes the new records and indexes to free segments, without
changing the current root. Firmware verifies the new indexes and checks that
the result retains every old live entry. Only a personal merge may replace a
personal translation. Base extents, checkbox states, IDs, and order are kept.
The second catalog publication retires the old extents after the new root is
durable. A power cut can leave the previous or the new root selected. A damaged
alternate catalog can put the store into read-only recovery.

Compaction packs survivors from partly stale journals into at most one fresh
journal per pass. It preserves the current append journal and the sequence
boundary. A pass that needs to copy records selects at least two victim
journals so that it returns space. It refuses if the replacement indexes and
working space cannot fit alongside the current store. This is an explicit USB
command. Automatic selection and scheduling on the keyboard are future work.

Pop shows Import or Saving during the operation. Input entered during exclusive
write steps is discarded rather than replayed in a burst. The left half reboots
after publication. The NOR tests cover both operations across staging, both
catalog commits, and partially erased sectors. Each operation passed 184
torn-write cases with the previous or fully updated state recoverable.
A separate 20,000-entry host run used 23 staging segments, 49.0 MB of reads
through publication and 100.5 MB for cold verification and lookup of every entry.
Those counts exclude USB reads used to gather the current snapshot.
On Boardsource, the exact CI Native build uploaded 256 temporary entries in
about 22 seconds including reboot. A second import replaced 253 of those in
about 31 seconds. Compaction retired three journals and increased free space
from 172 to 174 segments. All 256 test translations, the three existing
personal entries and the original checkbox selection matched after reopening.
The temporary entries were then removed and the original personal export matched.
A USB polling race was found after reboot: the flash could report READY before
the dictionary console registered. The client now waits for both.
One Boardsource power-loss trial interrupted the 182nd USB staging write for
a temporary import, before either catalog was published. After battery and USB
power were restored, both catalog headers matched the pre-test copies, the
selected generation was still 23, and the three personal entries and checkbox
settings matched. KAT resolved to cat, and full store verification passed.
A separate catalog publication trial used a temporary firmware hook
(`40c5dd3`) to pause after the inactive slot and its replacement payload
were written and verified, just before writing the commit word. The owner
disconnected USB and battery power during the pause. Cold boot selected
generation 23 with the original catalog header, all three personal entries
and the saved checkbox states. The other slot held a CRC-valid header for
generation 24 with an erased commit word. The temporary entry was absent,
KAT resolved to cat and full store verification passed. This covers the
pre-commit boundary on hardware.

A separate Boardsource capacity trial allocated 171 empty test journals in
the real shared pool, leaving six free segments. The board reopened with
the original entries, selections, and KAT lookup intact. The normal bulk
transfer refused an update that could not fit beside the current catalog,
with zero erase, write, or commit commands sent by that transfer. A direct
staging probe erased three free blocks and the firmware refused a fourth,
preserving its three-segment reserve. Republishing the original catalog
payload restored all 177 free segments. Cold reopen, the personal export,
checkbox selections, KAT, and full integrity verification passed. This
tests allocation pressure with sparse test data. Dense near-full lookup
latency was not measured. The host scan took several minutes because it
read the full contents of allocated journals, including empty ones.

Before the trial, the board briefly reported unavailable after a failed
export and two power cycles. A read-only diagnostic firmware boot then
validated both original catalogs and recovered access without changing flash.
The cause of the initial failure is unconfirmed. After the staging power-cut
trial, the original CI firmware (run `44466950711`) was restored and opened
generation 23 with all three personal entries and the saved selections. After
35 seconds idle, KAT still resolved to cat and the warm-resume counter
increased from zero to one while full opens stayed at one. Continue checking
cold boot and automatic wake behavior before promoting Native.

## Named dictionaries and lookup priority

A named import allocates a stable ID and uses the same staged publication as
bulk personal import. Existing dictionary rows, base references and live entries
are checked before publication. A priority move validates a permutation of the
current rows and retains the original journal and index extents, including
uncheckpointed edits. Both commands reboot the left so Javelin builds the list
in catalog order. Names must be unique and are limited to 44 ASCII characters.
The count limit comes from the running firmware.

On Boardsource hardware, the CI image from run `44477035567` moved Plover
Base above Lapwing. After reboot, `STKPWAO*EPBG` resolved to Plover's “zinc”
instead of Lapwing's “zink”. Restoring the original order and enabled flags
restored “zink”. All three personal entries matched the pre-test backup.
Named creation was exercised by host and fault-injection tests.

## USB manager and existing website

Javelin Tools continues to use its current HID/BT connector, dictionary list,
checkboxes and personal-edit endpoints. No changes to the externally hosted site
are required. A small native manager adds dictionary-qualified operations:

```sh
python3 tools/storage/native_manage.py --status
python3 tools/storage/native_manage.py --import personal.json
python3 tools/storage/native_manage.py --dictionary 6 --import lapwing-overrides.json
python3 tools/storage/native_manage.py --dictionary 6 --remove KAT
python3 tools/storage/native_manage.py --dictionary 6 --reset-outline KAT
```

The developer manager imports entries one at a time. The complete input is
validated before opening the keyboard. Each entry uses a retry-safe bounded transaction, supports empty
text, newlines and up to 2,047 UTF-8 bytes, and is committed only when complete.
Busy waits follow the reported maintenance step counter, refusing after three
minutes without observed progress or one hour total for an entry. On error the
tool reports the last saved point and does
not clear another dictionary. `--remove` hides that dictionary's outline, allowing
lower-priority dictionaries to win; `--reset-outline` exposes its compact base.
These are overlays, not a replacement upload of the whole compiled theory.

The existing format-3 bulk importer deliberately refuses this development format.
Use the native manager for its imports. Staging a large compact replacement or a
named dictionary uses the separate staged workflow above.

## Verification so far

- Actual pinned Javelin engine: native personal/base priority, tombstone/reset,
  multi-stroke, repeat/undo, persistent checkbox changes, lookups during native
  maintenance, and failure refusing lower-priority fallback.
- Native facade: reboot/reopen, older-root retention beyond 256 later edits,
  metadata-only retargeting, atomic fresh-ID reset with pinned old readers,
  auto-checkpoint/rollover, deduplicated export and corrupt-store refusal.
- Factory image: every one of **291,403 translations** checked after relocation.
  Real C++ provisioning/session code opens the generated catalogs and bases,
  preserves the reserved tail, and refuses damaged committed data without erase.
- USB staging: maximum UTF-8, split chunks, empty strings, commit retries, Busy
  retry, invalid NUL and incomplete-entry refusal.
- Explicit clean provisioning: erase ordering, page/readback verification,
  idempotent retry, final CRC and out-of-pool refusal.
- Input buffering: nested pauses, retry, timestamp wrap, mode cancellation,
  overflow recovery and 50,000 FIFO batches. Queued physical keys also pass
  through the actual Repeat → AllUp → JeffModifiers → Javelin pipeline for
  translation, repeat, undo and multi-stroke. The real Practice state machine
  tests queued on-time input after a delayed Speed timer and deferred advance.
- Dead-journal reclamation: 275 simulated torn-operation cases, retained-reader
  publication refusal, protected sequence/reset boundaries, empty-writer retention,
  actual reuse after both catalogs/readers release, and continued translation in
  read-only fallback. No old journal is erased during pruning. The actual pinned
  engine also translates/repeats from a read-only recovered catalog.
- Maintenance scheduling: time and step caps, immediate input priority, clock
  wrap, terminal result propagation and yielding after a slow erase. Import client
  tests cover continued progress beyond three minutes, stalled work, and the
  absolute one-hour limit with a simulated clock.
- Outline-length summaries: zero-read empty/wrong-length misses, pinned snapshot
  isolation, remove/reset semantics, full 16-stroke bounds, cold reconstruction and
  fresh-ID personal reset. Summaries are conservative until stale journals leave
  the catalog; they never suppress a potentially present outline.
- All earlier native catalog/checkpoint/rollover fault tests remain enabled.

The full-engine integration passed CI for `d1667bb`, buffered input for `08d5eb6`,
wake collection synchronization for `ec4c007`, and metadata stack reduction for
[`18e21a9`](https://github.com/disarmyouwitha/StenoSquid/actions/runs/44425344006).
Each gate includes both native boards, normal variants, host regressions and Linux
sanitizers. Main has not been switched to this storage implementation.

The reclamation/lookup local ARM build links at 465,912 bytes of internal flash
and 230,942 bytes of RAM (88.10%). Metadata reset/checkbox frames dropped from 2,136/2,096
to 760/720 bytes by reusing existing scratch, with no added RAM. Static frame sizes
are not runtime stack peaks; contention and peak measurements remain hardware
acceptance work.

## Index retention under staging pressure

Before allocating checkpoint indexes or another journal, the facade now compares
ownership in both validated catalog copies. If the operation already fits, it
keeps the older generation and adds no extra catalog writes. If staging is blocked
by extents unique to that older copy, it first verifies that retiring them would
make the allocation fit. Only then does it publish an exact copy of the current
payload into the alternate catalog and retarget the live cursor without replay.

This retains two valid catalog copies while allowing the obsolete index pair to
be reused. The copied payload must match the source CRC before its new header can
be committed. Slot pins can refuse retirement or the subsequent checkpoint before
any staged outputs are written. A successfully committed mirror remains the live
selection even if subsequent work is blocked. A possible commit/readback failure
stops edits until a validated reopen.

The prepare phase runs on the engine worker, supports normal read snapshots, and
blocks edits just like the checkpoint itself. No new dictionary-sized buffer or
on-flash format was added. A low-space operation can need one additional 64 KiB
catalog erase; ordinary operations with sufficient space retain the previous wear
cost. Imports count actual completed steps/generation changes as progress, rather
than treating repeated Busy/prepare state changes as forward progress.

Local strict-NOR tests cover 380 torn mirror/index/catalog operations; a constrained
pool where direct three-pair checkpointing refuses but the prepared two-pair path
succeeds; 24 edit/checkpoint/reboot reuse rounds; source and destination reader
pins; ambiguous mirror commit; changed-source CRC refusal; and unchanged catalog
commit counts when staging already fits. Retention passed [CI 44432909976](https://github.com/disarmyouwitha/StenoSquid/actions/runs/44432909976)
at `df6349e`, as well as the complete local suite. Hardware acceptance follows.

## Sorted recent edits and a reproducible import experiment

The 256-entry recent-edit address buffer is now sorted in RAM by dictionary/outline,
with equal keys in commit order. Its size and checkpoint limit are unchanged. Flash
journals remain chronological in the same format. Recovery reconstructs the sorted
RAM view; edits insert an address after existing equal keys. Binary search selects
the newest equal row and fully CRC-checks that selected payload once. Snapshots
retain their own address order. Invalid probes fail closed instead of becoming misses.

This removes the linear tail scan from normal lookups and checkpoint validation,
and avoids sorting forward keys through repeated flash reads. Existing 700-edit /
22,400-lookup oracle, snapshot, power-cut and malformed-record regressions pass.
The full-tail long-translation miss now requests 504 bytes instead of 14,336;
a 256-duplicate lookup validates only the selected newest 2 KiB payload.

A developer-only harness now runs the actual native store/allocator against strict
simulated NOR and the real factory image. It caches 32 CRC ranges like the firmware,
reconstructs a fresh store/device, then verifies every committed entry—including
when an import is safely refused partway through. It never opens a keyboard.

```sh
python3 tools/storage/bench_native_import.py \
  --input build/plover/plover-main.json --limit 5000 \
  --report build/native-import-report.json
```

Use `--limit 0` for the full source. `--storage-headers PATH` can compare archived
headers without changing the working tree. Reports include raw-input, encoded-input,
factory-image and storage-source SHA-256; compilation uses the selected header tree.
Sources changing mid-run invalidate the report. The small automatic regression also
checks that an alternate header directory actually controls compilation.

[Recorded 5,000-entry comparison](native-import-benchmark.json), with the same input
and factory image in each run:

| Metric | Retention baseline | Sorted recent edits |
| --- | ---: | ---: |
| Requested read bytes, including reopen/verification | 2,046,316,525 | 578,205,661 |
| Read calls | 171,196,875 | 48,811,616 |
| Maintenance steps | 1,064,796 | 770,480 |
| Program calls | 22,776 | 22,776 |
| Journal/index segment erases | 44 | 44 |
| Catalog erases | 24 | 24 |
| Entries verified after fresh reconstruction | 5,000 | 5,000 |

The approximately 72% read reduction adds no RAM or flash writes. The local ARM
link remains at 230,942 RAM bytes; flash is 469,528 bytes. These are requested IO
counts, not wire-alignment/cache measurements, MCU timing, power or typing latency.
Maintenance still rewrites/verifies indexes repeatedly during per-entry bulk imports;
staged bulk import remains worth pursuing even after this reduction.

## Full editable Plover host experiment

The same strict-NOR runner completed all **147,424** canonical outlines from the
pinned Plover JSON, merged into the personal dictionary while all six factory
compact bases remained installed. A fresh device/store instance reconstructed the
catalogs and verified every committed translation with no writes during checking.
The protected final sector was unchanged. [Raw report](native-import-benchmark.json)
includes source/dataset/image/storage SHA-256 and the exact counters.

- Nine 64 KiB pool segments remained free: 589,824 bytes, including the allocator's
  three-segment reserve (six segments / 393,216 bytes beyond that minimum).
- 709 maintenance jobs, 758 catalog erases and 1,963,404 program calls.
- 518,999,124,259 requested read bytes in total; 517,283,299,860 during import and
  1,715,824,399 during fresh reconstruction plus the complete lookup oracle.
- Approximately 19 minutes of host import time. This is not keyboard throughput.

This validates one real 16 MiB allocation/fragmentation case; it is not a universal
entry-capacity promise. It also exposes a bulk-performance blocker: repeated
whole-index work during per-entry imports remains expensive even after the sorted
recent-edit improvement. Do not infer that the full JSON is a practical HID import
from a host-time result. A staged bulk/compact import should build and validate
indexes once, preserve unrelated dictionaries, and atomically publish the result.
It needs its own space/refusal, reader and power-cut tests before deployment.

## Guarded same-boot idle resume

An already healthy native mapper now tries a same-boot resume after quad/DMA access
is restored. It requires an open, ready store, no maintenance, writable/non-quarantined
catalog state, matching selected/live roots and no retained lookup/export leases.
Both catalog slots are inspected through DMA: committed metadata must match a
trusted RAM root; the selected root must match exactly. The six mapped base probes
then run before READY is returned. Root changes, IO/corruption, pins, unfinished work
or an uninitialized instance refuse the shortcut and take the existing full path.

This is a retained-RAM/data-retention trust path; it does not claim to CRC every
journal/index payload on each idle wake. Full reboot/deep sleep starts with no
healthy mapper RAM and still performs full native catalog/journal/index validation.
Explicit verify remains available. Installation and detected faults clear readiness,
so they cannot reuse an old map. The shortcut adds no flash writes, replays no log,
does not reconstruct the engine, and retains saved dictionary selection.

`native_manage.py --status` now includes `warm_resumes` and `full_opens` (RAM counters
that reset on reboot). Local portable tests check bounded metadata-only reads,
read-only behavior, saved flag/maintenance transitions, retained pins, damaged
metadata, unknown generations and cold-instance refusal. Base probe/power behavior
still needs the real-board idle/deep-sleep tests. The local ARM link uses 230,950
RAM bytes, eight more than the sorted-tail build.

The idle-resume build passed [CI 44438639149](https://github.com/disarmyouwitha/StenoSquid/actions/runs/44438639149)
at `e925abb95186660f60185b31122f012b7c8624fe`: all normal/native board variants,
complete host tests, rendering and sanitizers. Exact downloaded UF2 family IDs,
block numbers, bounds and checksums were verified under `build/native-engine/warm-ready/`.

| Latest Actions profile | Flash bytes | RAM bytes | UF2 SHA-256 |
| --- | ---: | ---: | --- |
| Boardsource-Left-Native | 470,000 | 230,950 | `663f09847a04997c33e6aac3b4642c1ee91a39f64bfba0c4d7365dbabc8f6e17` |
| NiceNano-Left-Native | 471,232 | 231,046 | `d50773823e9002900a55a89bda908e4f985a2ff9f24c2adf53d56de7c030a44c` |

The matching Boardsource right UF2 remains byte-identical to the installed image
(`6734baad5ae72e828a2cd472b7225caa79a4d9f8382a5907baae1bcf6b473598`).
There is no new external-flash format change across these native checkpoints.

The sorted-tail build itself passed [CI 44436284566](https://github.com/disarmyouwitha/StenoSquid/actions/runs/44436284566)
at `7fb9840`, including all normal/native variants, host tests and sanitizers.

## Verified integration baseline artifacts

Source [`634255b`](https://github.com/disarmyouwitha/StenoSquid/commit/634255b2091cd34d1af894e1a14fe8826dff5239)
passed [CI 44428306440](https://github.com/disarmyouwitha/StenoSquid/actions/runs/44428306440):
all 14 firmware/probe builds, the complete host suite, rendering and Linux
ASan/UBSan gates. The complete local suite also passed. Downloaded UF2 block
numbers, nRF52840 family, address ranges, image CRC and SHA-256 were verified.

| Actions profile | Internal flash bytes | Linked RAM bytes |
| --- | ---: | ---: |
| Boardsource-Left-Default | 448,240 | 207,710 |
| Boardsource-Left-Native | 465,932 | 230,942 |
| NiceNano-Left-Native | 467,164 | 231,038 |
| Boardsource-Right-Default | 442,748 | 88,928 |

Native adds 23,232 bytes of linked RAM over Boardsource Default, including resident
wrappers, maintained reader/job snapshots and the 4 KiB input queue. The 32 KiB
configured heap is already included in those totals. Unlinked RAM is not a
measurement of available runtime heap or stack headroom.

Verified SHA-256:

```text
Boardsource-Left-Native  c096651d1b0687b62c3c50090cb5cc849b08f6dd6cf6324421e96ea33f8e796d
NiceNano-Left-Native     7f101de77dc145bd90239a96d04c147419fc254a422a460c14e13d74e917a9d2
Boardsource-Right       6734baad5ae72e828a2cd472b7225caa79a4d9f8382a5907baae1bcf6b473598
Native pool image       1c95cb7d6551f7791a94a61e378e2eded1b563041f65905147a6bfc66431e2e2
```

The right image is byte-for-byte identical to the currently installed RHS image.
Only the left needs flashing for this integration. Exact downloaded artifacts and
a verification manifest are held locally under `build/native-engine/ready/`;
the source/CI links above are the reproducible public reference. No firmware or
external-flash changes were applied to the connected board during this solo work.

## Large editable-import capacity is still a gate

The 200,000-entry synthetic test target is **not** a capacity promise. Native records include
identity, sequence, commit/CRC, outline and text; checkpoints must also protect the
index pair referenced by each catalog while staging a new pair. In the worst case
that meant three forward/reverse pairs at once in the verified baseline. The
retention follow-up can drop that to two under pressure, with an extra catalog
commit. Journals containing live entries
cannot yet be relocated away from stale neighbors.

Allocation arithmetic for importing the complete pinned JSON again as editable
personal entries, while keeping all six compact bases installed:

| Input | Entries | Journal segments | Segments per index | Baseline: three pairs | Retention: two pairs |
| --- | ---: | ---: | ---: | ---: | ---: |
| Lapwing Base | 114,885 | 107 | 8 | 234 of 255 | 218 of 255 |
| Plover Base | 147,424 | 134 | 9 | 267 of 255: exceeds pool | 249 of 255: fits arithmetic |

This model packs `align4(40 + 4*stroke_count + utf8_text_bytes)` records into
65,280 usable bytes per journal, reserves 74 segments for the six compact bases,
two catalog segments, the stated number of index pairs, and the three-segment
allocator reserve.
Sources and SHA-256 are pinned in the factory image manifest. These are allocation
estimates. The Plover row now has a completed host import/reconstruction result
above, but no full-scale on-device import. Other sources, edits, readers and retained
history can change allocation/fragmentation and capacity. The compact Plover base is already present; duplicating
it into an editable log is especially expensive. Compact replacement/import and
on-device qualification of retained-index staging remain future work. The manager stops and
reports its last saved entry on safe space refusal; it does not discard existing
committed entries to make a large import fit.

## Initial hardware results — November 18, 2026

The exact Actions images from run `44438639149` (firmware commit `e925abb`)
were flashed to the Boardsource left and right halves, identified by USB serial
before transfer. The right image is unchanged from its previous build. The clean
native installer erased/provisioned the shared pool, verified its complete CRC,
rebooted and reported READY with seven dictionaries. The reserved final 64 KiB
and internal settings were outside the install range.

Automated USB HID checks passed:

- KAT → cat and HE/HROE → hello.
- Personal add, replace, export and remove; the personal export is empty again.
- A dictionary-qualified long UTF-8/multiline override, hide and reset-to-base.
- Full native integrity verification after edits; KAT restored to cat.
- After 35 seconds with editor connections closed, lookup woke the flash and
  returned cat. Status reported `warm_resumes=1`, `full_opens=1`, `tail=6`,
  `maintenance=0`, `ready=true`, `read_only=false`, and no input overflows.
  This confirms the guarded same-boot idle path without another full open.

The owner confirmed the typing, Javelin, Plover HID and Practice smoke tests,
then passed the follow-up for Bluetooth editor changes, saved dictionary
selections after reboot, and typing after deep sleep with the right half
reconnected. Raw automated logs and the artifact manifest are local under
`build/native-engine/warm-ready/`.

## Checkpoint contention on Boardsource

The shorter catalog erase build (`1c4a7e9`) preserved all three personal entries
and the enabled dictionary selection through 12 manual checkpoints. The catalog
generation went from 9 to 21 and full integrity verification passed. The queue
held up to six key events with zero recorded overflows. The maximum maintenance
slice was 256 ms and the oldest recorded key event waited 250 ms before replay.
The owner saw typing pause and then release several queued words together.

Small forward and reverse indexes now erase only the first 4 KiB sector when
the complete worst-case output fits there. Larger indexes still erase full
segments. The right display uses **Saving** while Native maintenance owns the
flash, then restores its normal status. On the same Boardsource left half,
a checkpoint without typing on build `2cbfa0b` completed in 90 worker steps
and verified the original selections and all three personal entries. The
longest worker slice was 74.585 ms. After flashing the matching right image
from Actions run `44456710580`, a second typing trial completed 12 checkpoints
and advanced generation 22 to 34. The longest slice was 78.339 ms, and the
longest queued key wait was 64 ms. The queue held at most three events and
recorded zero overflows. All three personal entries, the current selection
and sampled translations matched before and after, and the full integrity
verification passed. This was a small dictionary and a short typing sample.

In a separate eight-checkpoint trial, the owner saw Saving briefly before the
display returned to Javelin. The typing trial's counters were sampled before
its final integrity scan. The next status showed a 7.615 s queued key wait, and
a timed standalone `user_dictionary_verify` took 7.966 s with no change in
checkpoint work steps. The integrity scan holds the dictionary lock, so a test
that types during that scan can see a much longer pause and subsequent replay
than during a checkpoint. Measure the two operations separately.

Automatic work and explicit USB commands are measured separately.
Ordinary typing must remain responsive across idle flash wake, background
integrity checks, and maintenance left pending after a dictionary edit.
Time the first chord after wake and compare the full editor output during
automatic work. Explicit install, import and Verify may take time. Check
that their status is visible, interruption is recoverable and ending the
operation does not release a burst of stale input.

## Hardware acceptance checklist

1. Flash the exact Left-Default Actions UF2 and run the clean installer. Confirm
   READY and the seven catalog dictionaries; right typing/display should reconnect.
2. Check ordinary typing, onboard KAT, multi-stroke HE/HROE, repeat/undo, Plover HID
   and offline Practice with a blank editor focused.
3. Add, replace, export and remove a temporary personal entry through the existing
   browser, including wireless editing. Toggle bases; confirm selection after reset.
4. Use the native manager for a small dictionary-qualified override. Test remove
   versus reset-to-base, and import a long/accented/newline entry. Restore test keys.
5. Reboot and unplug/reconnect; verify edits and flags persist. Then check idle
   flash off/wake and deep-sleep wake with no lost first stroke.
6. For the guarded idle path, record `warm_resumes` / `full_opens`, close editor
   connections, wait for the flash LED to turn off, then type KAT. A same-boot idle
   wake should increase `warm_resumes` without another `full_opens`. Normal reset
   or deep sleep resets the RAM counters and should take full validation again.
7. Reboot for fresh counters and record `user_dictionary_status`. While typing
   a known passage, start a native index rebuild with `user_dictionary_rebuild`.
   Compare the editor output against the passage and check that `work_steps`
   advanced, maintenance finished, and `input_overflows` stayed at zero. Record
   `max_maintenance_slice_us` (wall time inside the worker slice) and
   `max_replay_delay_ms` (time from a deferred key event to replay). These maxima
   reset at reboot. They complement the existing queue high-water count and do
   not include full keyboard-to-host latency. Capture runtime stack/heap peaks
   separately.

**Not yet complete:** automatic compaction scheduling and an overlong-tail rebuild;
compact base replacement and named dictionary delete or rename; physical
power-cut qualification; measured 200 WPM/burst contention and runtime peaks.
Wholly dead journals can now be reclaimed after ownership is released. Partially
stale journals still retain their allocation, so sustained growth can safely
refuse an edit when staging/reserve space is exhausted. Do not promote this build
to the normal release until those remaining acceptance gates are resolved.

## Live-record compaction: next design boundary

The next compactor can replay only still-current records out of a selected old
journal, using normal committed appends and bounded checkpoints. It must reserve
the victim independently of catalog-slot pins across multiple publications, preserve
dictionary identity and set/remove/reset semantics, and release that journal only
when both catalogs and all readers no longer need it. It also needs destination
and checkpoint headroom before starting; a victim with no reclaimable space should
not be copied merely to move bytes around. This is not implemented or enabled yet.
Full-scale import measurements and power-cut tests should guide that work rather
than assuming compact-layout arithmetic proves a safe cleaning schedule.

Compaction should initially exclude the active append journal and the journal
holding the current high-water sequence. Leaving an anchor until real edits move
it is preferable to inserting hidden no-op user entries. A RAM segment-exclusion
lease must protect the victim across maintenance jobs; pinning its old catalog for
the entire run would prevent later catalog-slot publications. A reset discards the
cursor and restarts from validated ownership rather than requiring a migration or
persistent cleaner-resume format.

Headroom needs a fresh destination journal plus the staged index pair and reserve.
The current checkpoint planner conservatively allocates old-count plus tail-count,
which can add a segment to each index at a boundary even when cloning existing
keys. That bound must be budgeted or tightened before automatic cleaning is enabled.
The allocator's three-segment minimum alone is not a qualified GC guarantee. A
victim with at most one journal's surviving records should need at most one new
journal, but each intermediate checkpoint must also fit without consuming its
own future working space.

Replaying current set/remove records with new sequence numbers leaves lookup
semantics unchanged. Flush within the 256-edit bound, perform a final checkpoint,
and release the victim only after catalog/read ownership allows it. Interrupted
runs may leave duplicate equivalent records; they must never leave missing or
resurrected translations. Test these properties with fault injection before
adding the coordinator to firmware.
