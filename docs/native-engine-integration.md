# Candidate B: full engine/store integration test build

Status: **opt-in native engine implemented; full engine checkpoint passed CI; journal reclamation follow-up in verification; hardware validation pending**.
Working branch: `prototype/native-journal-reclamation`, following
`prototype/native-engine-integration`. The CI-passing full-engine build is preserved
at `checkpoint/native-engine-integrated` (`ec4c007`); the following stack reduction
passed CI at `18e21a9`. Both descend from `prototype/native-checkpoint-job`. Main and normal firmware remain on
the production store until the new path passes hardware acceptance.

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

CI builds `Boardsource-Left-Native` and `NiceNano-Left-Native` alongside every normal
variant. Native images are development artifacts, excluded from public releases.
Only the left needs changing; the existing right/display protocol is unchanged.
The native format requires its own clean provision. A normal UF2 and a native
external-flash image are not interchangeable.

With Native firmware flashed, leave the left running over USB and close other
editor connections. Install the ordinary `hidapi` Python dependency, then run:

```sh
python3 tools/storage/native_install.py --status
python3 tools/storage/native_install.py --install --confirm-erase
```

The explicit confirmation flag replaces **all external dictionaries in the 255
pool segments**. It leaves the reserved final segment, internal ZMK settings,
Bluetooth pairing, Studio keymap, host profile and Practice progress untouched.
The tool builds the pinned image if none is supplied; it sends bounded USB HID
commands, verifies the full image, and waits for the normal reboot and READY.
Pop displays Install. No serial port, polling daemon or second provisioning
firmware is required. A stopped transfer releases its power hold after a minute;
partial storage remains unavailable until an explicit retry.

Native install refuses on normal firmware. The normal `base_install` path is not
available in Native firmware. Returning to normal firmware requires reprovisioning
its fixed bases with the existing repair/install tool; there is no converter.

A private pre-test personal backup was captured locally at
`build/native-engine/owner-personal-backup.json` (zero entries on the connected
keyboard). It is not checked in.

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

Imports merge entries one at a time. The complete input is validated before opening
the keyboard. Each entry uses a retry-safe bounded transaction, supports empty
text, newlines and up to 2,047 UTF-8 bytes, and is committed only when complete.
Busy waits follow the reported maintenance step counter, refusing after three
minutes without observed progress or one hour total for an entry. On error the
tool reports the last saved point and does
not clear another dictionary. `--remove` hides that dictionary's outline, allowing
lower-priority dictionaries to win; `--reset-outline` exposes its compact base.
These are overlays, not a replacement upload of the whole compiled theory.

The existing format-3 bulk importer deliberately refuses this development format.
Use the native manager for its imports. Staging a large compact replacement or a
new logical dictionary is future manager work, rather than a hidden behavior of
this small merge command.

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
- All earlier native catalog/checkpoint/rollover fault tests remain enabled.

The full-engine integration passed CI for `d1667bb`, buffered input for `08d5eb6`,
wake collection synchronization for `ec4c007`, and metadata stack reduction for
[`18e21a9`](https://github.com/disarmyouwitha/StenoSquid/actions/runs/44425344006).
Each gate includes both native boards, normal variants, host regressions and Linux
sanitizers. Main has not been switched to this storage implementation.

The reclamation local ARM build links at approximately 465 kB of internal flash
and 230 kB of RAM (87.8%). Metadata reset/checkbox frames dropped from 2,136/2,096
to 760/720 bytes by reusing existing scratch, with no added RAM. Static frame sizes
are not runtime stack peaks; contention and peak measurements remain hardware
acceptance work.

## Hardware acceptance when the owner returns

1. Flash the exact Left-Native Actions UF2 and run the clean installer. Confirm
   READY and the seven catalog dictionaries; right typing/display should reconnect.
2. Check ordinary typing, onboard KAT, multi-stroke HE/HROE, repeat/undo, Plover HID
   and offline Practice with a blank editor focused.
3. Add, replace, export and remove a temporary personal entry through the existing
   browser, including wireless editing. Toggle bases; confirm selection after reset.
4. Use the native manager for a small dictionary-qualified override. Test remove
   versus reset-to-base, and import a long/accented/newline entry. Restore test keys.
5. Reboot and unplug/reconnect; verify edits and flags persist. Then check idle
   flash off/wake and deep-sleep wake with no lost first stroke.
6. Exercise maintenance under active typing and record latency/drops, plus runtime
   stack/heap peaks. CI correctness does not establish the typing-contention gate.

**Not yet complete:** relocating live records to compact partially stale journals,
an overlong-tail rebuild;
compact import/replacement/delete/rename/reorder manager transactions; physical
power-cut qualification; measured 200 WPM/burst contention and runtime peaks.
Wholly dead journals can now be reclaimed after ownership is released. Partially
stale journals still retain their allocation, so sustained growth can safely
refuse an edit when staging/reserve space is exhausted. Do not promote this build
to the normal release until those remaining acceptance gates are resolved.
