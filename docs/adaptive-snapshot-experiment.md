# Adaptive dictionary snapshots experiment

Default Left firmware supports compact named snapshots and explicit personal conversion over USB. Compact images and journals use the same Native catalog and allocator. Existing pools using that layout open directly.

Start with the [hardware checkpoint and test sequence](#tonights-hardware-checkpoint-and-test-sequence) for the pinned `b9eba3f` build, fixture commands, and recovery procedures.

## Gate A results

J uses the current Native staged named import. S compiles the same input as JSC4 and rebases the image into a free contiguous run. Both start with the same six factory dictionaries and empty personal store. The initial catalog and extents occupy 75 of 255 usable segments, including the two roots. Both candidates retain the three-segment maintenance reserve.

| Added corpus | Journal added / total segments | Snapshot added / total segments | Snapshot compiled size | Same-size second-copy estimate |
| --- | ---: | ---: | ---: | --- |
| Pinned Plover, 147,424 entries | 152 / 227 | 36 / 111 | 2,339,620 B | J refused, S peaks at 148 segments |
| Pinned Lapwing, 114,885 entries | 123 / 198 | 28 / 103 | 1,830,440 B | J refused, S peaks at 132 segments |
| 100 legal two-stroke entries | 3 / 78 | 1 / 76 | 2,548 B | Both fit |
| 200,000 legal two-stroke entries | Refused, 298 estimated total | 30 / 105 | 1,931,928 B | S peaks at 136 segments |
| 6,900 entries with 1,120-byte text | 126 / 201 | 120 / 195 | 7,834,164 B | Both refused |

The Plover snapshot used 7.25 MiB less flash than the same staged journal import and matched all 147,424 translations. Dense long text saved only six segments, so the results support an experiment with selective snapshot use. A small existing journal may absorb a small import more efficiently than this staged fixture suggests.

The second-copy column models retaining one full copy while staging a second named row in the untouched fixture. It gives geometric feasibility, including the new metadata block, before edit churn. The packed-image figures show the occupied segments after writing a complete 16,711,680-byte pool image offline. They establish a capacity bound. A supported USB recovery workflow also needs a self-contained rescue bundle, end-to-end recovery tests and explicit consent before erase.

From this branch run:

```sh
python3 tools/dictionaries/prepare.py
python3 tools/bench/snapshot_feasibility.py --full --output build/adaptive-snapshot/results.json
python3 tools/tests/test_native_stage.py
```

[JSON measurements](benchmarks/adaptive-snapshot-gate-a.json) and [CSV measurements](benchmarks/adaptive-snapshot-gate-a.csv) record pinned hashes, compiled bytes, record and index estimates, and contiguous-run and total-space checks. All successful staged cases compared every translation after reconstructing the image. The original legacy `TestConstantCost(190000)` passed with 37 free segments, up to six program calls to add, one to remove, and 2,084 open reads. That fixture uses arbitrary 32-bit keys, so its result is a separate reference. The Native/JSC4 synthetic corpus round-trips through the real outline parser.

The strict NOR stage test cuts writes at 57 flash boundaries with two tear lengths and checks recovery, dictionary IDs/order/selections and unrelated entries. The firmware also checks staged extent CRCs and uses the normal Native dictionary adapter for forward lookup. Hardware latency and power-cut acceptance remain open. Gate A host timings do not predict typing latency.

## Optional named-snapshot smoke test

1. Use a spare left half or a disposable QSPI fixture with the ordinary Native dictionaries installed. Record the USB serial number, save a complete `stenosquid.py --backup-all` before introducing any snapshot rows, and keep the known-good left UF2 handy. Keep the matching right firmware running.
2. Use the matching Actions artifact, `Boardsource-Left-Default` or `NiceNano-Left-Default`. Each archive contains its own `zmk.uf2`. The new left can open an existing unmarked Native pool. Confirm normal typing and `KAT` before staging anything.
3. Connect by USB, close Studio/Javelin connections and run the preflight with a Plover JSON dictionary. The planner opens a staging session to account for both catalog roots. It reads metadata and releases the session without writing unless you add `--stage`. Do this while idle.

   ```sh
   python3 tools/storage/native_snapshot.py smoke.json --name 'Snapshot Smoke' --serial YOUR_USB_SERIAL --format snapshot
   ```

   A minimal fixture is `{"KAT":"snapshot cat","KAT/TA":"snapshot multi","PWOB":"snapshot Bob"}`. Append `--stage` to publish. Reconnect after reboot and check that the named row and its enabled state appear in Active Dictionaries. If a final acknowledgment is lost, reconnect and check the catalog before retrying. Reusing the same name is refused.
4. Check the new translations, then add an outline, replace a baseline translation and remove another from the named dictionary. Get its numeric ID with `stenosquid.py --list-dictionaries --serial YOUR_USB_SERIAL`. For the sample above, save `{"TAS":"snapshot added","KAT":"snapshot replacement"}` in `overlay.json`, then run:

   ```sh
   python3 tools/storage/native_manage.py --serial YOUR_USB_SERIAL --dictionary ID --import overlay.json
   python3 tools/storage/native_manage.py --serial YOUR_USB_SERIAL --dictionary ID --remove PWOB
   ```

   The new outline and replacement should win, and the removal should fall through to the next enabled dictionary. Move or toggle the dictionary to exercise priority, then cold boot and repeat the checks. After a test-only interrupted transfer, reboot and confirm that either the old catalog or the complete new catalog is visible.
5. Use `stenosquid.py --backup smoke-export.json --dictionary 'Snapshot Smoke'` to export the effective named dictionary. `stenosquid.py --backup-all snapshot-backup.json` creates a self-contained recovery bundle. Verify it offline with `python3 tools/storage/native_recovery.py snapshot-backup.json`.

## Complete backups

Version 2 records the selected binary catalog, the checked host layout, and the exact referenced extents with a SHA-256 checksum. This preserves compact snapshot and pinned image contents, pinned source digests, logical IDs, priority, checkboxes, and every live override and tombstone. It includes raw journal bytes, which can contain old translations that no longer appear in a dictionary export. Treat it as private. The CLI publishes the file only after validation and flush.

Offline reconstruction verifies catalog ownership, compact lookup parity, journal records and both indexes, then produces two valid roots in memory. It needs no downloads. The version 2 bundle is the full backup format. Restore it using the separate, explicitly confirmed USB recovery utility.

## Snapshot lifecycle

Rename and export work through Manager and the standard dictionary commands. Manager can also delete a snapshot after confirmation. The experimental CLI provides read-only planning for the following operations. Append `--stage` only after checking the proposed operation and retaining a backup.

```sh
python3 tools/storage/native_snapshot.py --name 'Snapshot Smoke' --serial YOUR_USB_SERIAL --consolidate
python3 tools/storage/native_snapshot.py --name 'Snapshot Smoke' --serial YOUR_USB_SERIAL --delete
```

Consolidation takes the current effective dictionary as the new baseline, then removes only that dictionary's live overlay references. It keeps its ID, position and enabled state. An empty effective dictionary cannot be consolidated into JSC4. Delete retires the dictionary and its compact extent without reusing the ID. Ordinary journal maintenance can later reclaim its stale edits. The allocator protects both roots, retained readers and the three-segment reserve. A capacity refusal leaves the previous catalog in place.

### Optimize in Manager

Connect the experimental left over USB with other editor sessions closed.
Select the personal journal or a user snapshot and choose **Consolidate edits**.
Consolidation folds saved edits and hidden outlines
into the compact image. Conversion compiles the existing personal entries.
Both keep the dictionary ID, position and enabled state. Factory revisions
use the dedicated pinned update workflow, which preserves personal entries
and catalog priority.

The preview compares the image and live edit bytes before and after, and shows
the change in allocated blocks and the staging cost.
It opens a temporary staging session and aborts it without programming flash.
**Compact** rechecks the named keyboard, catalog generation,
live journal boundary, protected segments and compiled plan before writing.
A changed plan needs a fresh preview. Publication can pause typing and reboots
the left. Insufficient space stops the operation before programming.

**Delete** on the personal dictionary removes its compact baseline and its live edits,
leaving an empty journal with the same personal slot, priority and enabled state
on firmware supporting `CLEAR1`. Type `CLEAR` to confirm. The other dictionaries
keep their entries.

Manager uses the JSON filename to suggest a dictionary name. Clear the name to
import into Personal. After a successful import, Manager opens a fresh flash map
when the keyboard supports it.

**Download full backup** captures a complete version 2 bundle even when
all user dictionaries are still journals. Verify it offline with
`python3 tools/storage/native_recovery.py stenosquid-backup.json`. The separate
serial-confirmed USB rescue procedure remains the restore path for that bundle.
**Compact journal** previews shared journal cleanup and marks the blocks it can
release on the flash map. **Apply journal cleanup** rechecks the plan before
writing. Compact images stay in place.

The on-flash catalog is `SQBCAT02`. Compact user rows reference a single-member
JSC4 extent and share the ID-qualified journal. A board on an older flash layout
needs a fresh install after flashing the new Left UF2. Clean provisioning
requires explicit erase confirmation.

Manager lifecycle and recovery commands require USB. The standard Javelin editor transport also supports Bluetooth.

## Edit churn and import preflight

The benchmark drives the production Native journal, rollover, and checkpoints through a strict in-memory NOR device. It applies the same deterministic additions, repeated replacements, tombstones, and UTF-8 text to both representations. Each measured checkpoint reopens the catalog and compares the exported effective dictionary with the independent expected result. The CSVs distinguish active extents from allocator protection across both roots and quantify bytes read, programmed and erased in the host simulation.

| Initial named corpus | Edits | Journal active / protected | Snapshot active / protected | Snapshot consolidation peak |
| --- | ---: | ---: | ---: | ---: |
| 10,000 | 100 | 87 / 87 | 78 / 78 | 82 |
| 10,000 | 1,000 | 87 / 89 | 81 / 81 | 85 |
| 10,000 | 10,000 | 98 / 100 | 91 / 93 | 99 |
| 50,000 | 100 | 133 / 133 | 83 / 83 | 92 |
| 50,000 | 1,000 | 133 / 133 | 86 / 86 | 96 |
| 50,000 | 10,000 | 143 / 151 | 96 / 98 | 110 |

After 10,000 edits to the 50,000-entry corpus, forward and reverse indexes occupied eight segments for the journal and two for the snapshot. Stale journal records totaled 575,588 bytes and 328,100 bytes respectively. No individual journal extent was entirely stale. The snapshot consolidation still had a 155-segment contiguous free run and needed 12 staging segments. These results describe the particular churn tape and layout. Timing and erase behavior on hardware are separate measurements.

The incremental control adds to a personal dictionary with an already active journal. At 100 additions there were **zero** additional active or protected segments, at 500 there were zero additional active segments and two additional protected index segments, and at 1,000 there was one additional active segment. Creating a new named dictionary atomically with the existing stage planner can consume more than adding these edits to an existing journal.

Run the benchmark against prepared pinned dictionaries.

```sh
python3 tools/bench/snapshot_churn.py --output build/adaptive-snapshot/churn.json
python3 tools/bench/snapshot_churn.py --baseline 50000 --output build/adaptive-snapshot/churn-50k.json
```

[10K fixture results](benchmarks/adaptive-snapshot-churn.csv) · [50K fixture results](benchmarks/adaptive-snapshot-churn-50k.csv)

On the experimental firmware, Manager previews named imports using both host planners and shows the bytes, added segments, staging cost, current free run and any refusals. Automatic prefers a compact image if it saves steady-state segments and either at least 1 MiB or at least one segment of logical bytes and 25% of journal bytes. A compact plan also needs space for a same-size consolidation under the current layout. Manager highlights the automatic recommendation. Select either eligible format card to override it before staging. The approved plan is recomputed before publication. Source changes, appended edits, changed catalogs or different occupied segments invalidate the approval. Leave the name blank to keep the existing personal merge path.

## Read-only flash map

Choose **Scan layout** in Manager while the keyboard is idle and awake. The grid shows all 256 segments, highlights extents referenced by both roots, and distinguishes older-root-only journals and indexes from current ownership. Hover over a segment to see both roots' descriptors. Unreferenced space comes from the catalog geometry. The view does not verify flash contents or scan journal records for stale entries. The three reserve segments are deducted from total staging capacity and are not pinned to fixed addresses.

The endpoint has no polling, input pause, staging lease or write path. It defers when flash is asleep, the lock is busy, input is pending, or its short metadata-read budget expires. A missing or unreadable root in recovery makes free space unknown. The physical timing and typing behavior of this button still need a board check.

## Extended named-snapshot checks

1. Complete the [initial smoke test](#optional-named-snapshot-smoke-test) on the matching spare left. Keep the existing right firmware. In normal typing and Javelin, check both halves and one multi-stroke translation.
2. Save a version 2 backup after creating the compact dictionary and adding the three sample overlay edits. Verify it offline and export the named dictionary. Check that `KAT` is the replacement, `TAS` is present, `PWOB` is absent, and the untouched multi-stroke entry is present.
3. Rename the snapshot in Manager, move it below an enabled factory dictionary, and toggle it off and back on. Check lookup priority, name and flags after a cold boot. Move it back above the factory dictionary for the remaining checks.
4. Export the snapshot, run `--consolidate`, and export again. The two dictionaries should match. Click **Measure saved edits** and confirm the snapshot's overlay count is now zero. Confirm the numeric ID is unchanged.
5. Scan the layout before and after staging or deleting the test snapshot. Inspect the snapshot extent and free-run count. Request another scan while typing and check whether the request defers or completes without delaying text. On the named import form, check the automatic recommendation and select the other eligible format before staging. Upload a different same-size file and check that the old approval does not carry over.
6. For a **test-only power cut on the spare**, use a larger throwaway named import or snapshot consolidation to allow time to interrupt staging. With a verified host bundle and an exported copy of the test dictionary safely on disk, disconnect USB and switch off its battery during data staging, then reconnect. Record whether the old or complete new catalog appears and compare the effective export and priority. Repeat near the catalog publish message if the window is observable. For ambiguous acknowledgment, reconnect and inspect the ID and generation before retrying. Do not infer physical cut coverage from console progress alone.
7. Delete the disposable snapshot through Manager. Confirm its row disappears and an unrelated dictionary and its edits survive reboot. Check that wired setup, normal Javelin WebHID, typing and Practice still work.

Full Gate B still needs physical cutoff timing, dense-store and sustained high-speed typing measurements, plus USB recovery on a blank or partly written pool. Direct conversion of an existing named journal and the extended lifecycle commands over BLE are outside this build.

## Personal conversion

Run the experimental CLI with the USB serial of the spare left.

```sh
python3 tools/storage/native_snapshot.py --convert-personal --serial YOUR_USB_SERIAL
python3 tools/storage/native_snapshot.py additional.json --convert-personal --serial YOUR_USB_SERIAL
```

Append `--stage` after reviewing the contiguous extent requirement and the total staging segments. The second form fills missing outlines from Plover JSON. Local sets and tombstones win. The same catalog row keeps its ID, name, priority and enabled flag. Existing sets become the compact baseline and existing tombstones stay in the shared overlay. Further adds, replacements and removals use that overlay. Use `--consolidate --name user_dictionary` to fold subsequent edits into a new baseline. The bulk clear command remains unavailable for compact personal dictionaries.

The [personal capacity report](benchmarks/personal-snapshot-capacity.json) validates every translation for Plover and the legal 200,000-entry target. A nearly full journal may leave too little contiguous room to preserve the old root while staging. The full Plover journal fixture left 29 contiguous segments and was refused. Both large compact images fit on the pinned factory pool using fill-only conversion, with no clean install. A fixture with 158 free segments but a seven-segment largest run refused both images. A small real Native transaction recovered across 92 simulated flash boundaries with two tear lengths, preserving the disabled flag, priority, ID and tombstone.

## Lean compact storage and search capabilities

User snapshots are single-member JSC4 images ending in SHI1. That gives compact
forward lookup and lightweight hints at the original compiler's exact footprint.
The catalog keeps its existing snapshot marker. User snapshots with other
trailers are rejected at open or staging. Backup, export and recovery require
the same lean format.

| Path | What it supports |
| --- | --- |
| Javelin compact map | Exact forward lookup with per-outline-length hash tables, occupancy bitmaps and 24-bit rows, plus entry enumeration for export. Upstream partial-outline search enumerates rows of eligible lengths. Upstream exact reverse and prefix methods consume candidate entry addresses supplied by a separate reverse-map layer. The plain forward tables cannot produce these candidates efficiently by text. |
| StenoSquid SHI1 | An exact translation can suggest one preferred single chord. The compiler selects the candidate with the fewest keys, then the smallest mask. There is no multi-stroke candidate, prefix enumeration or list of all equivalent single chords. The Native adapter checks that the selected outline still resolves to this dictionary and is visible through overlays. |
| Native journal and overlays | Forward and reverse checkpoint indexes with a bounded live tail preserve current sets, replacements and tombstones. The reverse index accelerates exact text for current journal sets. Prefix and partial-outline queries enumerate live journal entries, retaining their existing result limits. |
| StenoSquid compact baseline | Forward lookup, Practice, export, and SHI1 hints. Exhaustive reverse, prefix and partial-outline search of compact baselines is unsupported. The HID console returns `ERR Command unavailable on this keyboard` for `lookup`, `lookup_prefix`, and `lookup_partial_outline`. Exact forward `lookup_stroke` still works. |
| USB host | Export a dictionary to search all of its effective translations and outline sequences. Export merges that dictionary's baseline with its own overlays. Use the saved catalog order and enabled flags to reconstruct cross-dictionary winners. For any future on-device scan, a result cap alone cannot bound no-hit latency. A scan would need a fixed work budget, continuation and cancellation that yields to typing. No such scan is enabled. |

The StenoSquid Native adapter reads compact data through QSPI-safe accessors.
It does not call the generic Javelin compact-map partial scan against these
snapshots. The upstream source audit used the pinned Javelin implementation.
[Compact map](https://github.com/disarmyouwitha/javelin-steno/blob/de5a629bb383581882dfd6a5b2b5b9b44b71dc9c/dictionary/compact_map_dictionary.cc)
· [Reverse-map layer](https://github.com/disarmyouwitha/javelin-steno/blob/de5a629bb383581882dfd6a5b2b5b9b44b71dc9c/dictionary/reverse_map_dictionary.cc)

### Capacity gate

| Same input | Original JSC4 + SHI1 | Abandoned indexed experiment | Corrected default | Conversion / consolidation staging |
| --- | ---: | ---: | ---: | ---: |
| Plover, 147,424 | 2,339,624 B / 36 segments | 4,318,524 B / 66 | 2,339,624 B / 36 | 37 / 37 segments |
| Synthetic, 200,000 | 1,931,932 B / 30 segments | 4,339,360 B / 67 | 1,931,932 B / 30 | 31 / 31 segments |

Each segment is 64 KiB. Neither corrected image adds any per-image metadata
beyond JSC4 and the existing SHI1. The pinned-factory, empty-overlay fixture has
one shared journal segment and zero forward or reverse checkpoint extents.
The Native catalog uses two root segments and the measured 800-byte catalog
payload needs one separate staging segment. Existing overlays and indexed
journal records add their normal shared storage and may add staging segments.
Each update still needs to leave the three-segment maintenance reserve free.

CI compiles the same data through the original builder and the default
snapshot compiler, requires byte-for-byte equality and the exact size
budgets above, and checks every forward lookup and export after relocating and
staging both corpora. Run it after preparing the pinned dictionaries.

```sh
python3 tools/tests/test_native_compact_capacity.py
```

[Capacity gate output](benchmarks/lean-snapshot-capacity.json)

## USB recovery on a spare

Verify the bundle offline with `python3 tools/storage/native_recovery.py experiment.json` first. On an expendable spare running the matching experimental firmware, close the editor, connect over USB, and confirm its serial. Recovery erases the spare's shared dictionary pool when it differs from the bundle. It leaves the reserved final segment outside the transfer. Use the same serial twice so a mismatched target cannot start.

```sh
python3 tools/storage/native_recovery.py experiment.json --restore \
  --serial SPARE_USB_SERIAL --confirm-erase SPARE_USB_SERIAL
```

The host validates every extent before contacting the board. The firmware accepts the recovery transport without a mounted Native catalog. It erases the pool, writes and checks the content with the catalog commit words still erased, then publishes each root and verifies the complete image. After reboot, the host verifies dictionary IDs, order and flags before applying and reading back the saved host layout. If the final acknowledgment is lost, a matching image can finish without re-erasing. If the store remains incomplete, reconnect and explicitly repeat with the same bundle. No destructive retry is automatic.

For physical qualification, test an initially blank spare and interruptions during **Erasing**, data transfer, and the **Verifying** message. Record the point reached, reboot with USB and battery restored, and repeat the command. Verify the exported test dictionary and overlays, dictionary ordering, checkboxes, and host layout against the bundle. Inspect the physical catalog after a near-publication cut if the timing is observable. Host fault injection covered 138 boundaries in two initial states and with two tear lengths. Neither Manager nor automatic import selection offers destructive recovery.

### Import eligibility

The preview distinguishes a same-size copy-on-write replacement, an initial
image that fits but would need more space for a future consolidation, and an
initial image that cannot stage. The second case remains gated. Experimental
USB recovery restores a verified bundle exactly. Manager offers no destructive
fallback. See the checklist below for the expected editor search differences.

### Offline repacking

Repacking is a host operation. It relocates lean JSC4 images and their SHI1
hints, rebuilds the shared Native journal and both indexes, and leaves three
free segments for maintenance. Journal-only user dictionaries are converted
only when the packed plan uses fewer segments. Other overlays and all live
tombstones keep their semantics. The new pool contains two fresh copies of
the optimized catalog. Superseded journal records, the old alternate catalog
and unreferenced flash data are omitted from the new layout. Keep the original
complete backup as your recovery copy.

Run these commands from the repository root with the keyboard idle during
capture. Use a private directory that will survive a reboot. The commands
require new output paths.

```sh
# Read external flash. Write the original bundle to the computer.
python3 stenosquid.py --backup-all /path/to/original.json --serial SOURCE_USB_SERIAL
python3 tools/storage/native_recovery.py /path/to/original.json

# Offline. Build a separate, self-contained version 2 bundle and layout report.
python3 tools/storage/native_repack.py /path/to/original.json \
  --bundle-out /path/to/optimized.json --report /path/to/repack-report.json
python3 tools/storage/native_repack.py /path/to/original.json \
  --verify-target /path/to/optimized.json

# Optional. Read-only check that the source keyboard still matches its backup.
python3 tools/storage/native_repack.py /path/to/original.json \
  --check-live-source --serial SOURCE_USB_SERIAL
```

The report includes both file hashes, the target pool SHA-256, source capture
identity and time when available, every dictionary's export hash and entry
count, and the number of verified active priority winners. Source layout
figures in the offline report describe the selected catalog saved in the
backup. The live check also reports free space across both retained roots.
The report distinguishes storage savings, a larger contiguous run with the
same usage, and a recommendation of no operation. Its staging figures come
from running the current allocator against each target snapshot at its
current image size. Future edits, retained roots and image growth can change
those results. Use `--keep-representations` to keep all current formats, or
`--convert NAME` to restrict candidates. `--image` produces a raw fake-NOR
test fixture only.

For the 147,424-entry Plover personal journal, repacking used 111 segments
instead of 226 and expanded the largest free run from 29 to 144 segments.
All 438,827 effective per-dictionary entries and 243,093 active priority winners
matched. An already lean 200,000-entry personal snapshot kept its 105 used
segments. [Repack capacity report](benchmarks/offline-repack-capacity.json)

#### Optimized USB recovery on an expendable spare

This path is awaiting physical qualification. It accepts an unprovisioned
spare, or a spare already matching the exact optimized image for an
idempotent retry. The source check can prove equality at capture time. The
current firmware releases that freeze before opening an erase session, so
a live pool cannot be safely replaced by this tool. Manager and Automatic
import keep their existing staging refusal.

1. **Offline verification.** Save the original and optimized bundles as
   separate files and verify both with the commands above. Record both file
   digests and the expected free run. Keep both files throughout testing.
2. **Identify the spare.** Disconnect the source half and close other editor
   connections. Connect only the expendable left by USB. Confirm the serial
   and `NATIVE2 UNPROVISIONED` using `python3 stenosquid.py --status --serial
   SPARE_USB_SERIAL`. Stop if it reports READY for an unrelated pool. The
   optimized restore is deliberately refused in that state.
3. **Explicit pool erase.** On that spare only, repeat the exact serial and
   the optimized **file** SHA-256 shown by offline verification. The next
   command erases the entire external dictionary pool, leaving the reserved
   final segment outside the transfer.

   ```sh
   python3 tools/storage/native_repack.py /path/to/original.json \
     --restore-spare /path/to/optimized.json --serial SPARE_USB_SERIAL \
     --confirm-erase SPARE_USB_SERIAL:OPTIMIZED_FILE_SHA256
   ```

4. **Cold boot and readback.** After the command confirms the full-pool CRC
   and catalog, disconnect USB and turn off the spare battery. Power it back
   on and reconnect USB. Before editing or requesting maintenance, run:

   ```sh
   python3 tools/storage/native_repack.py /path/to/original.json \
     --check-live-target /path/to/optimized.json --serial SPARE_USB_SERIAL
   ```

   Expect READY, a byte-for-byte match for the active catalog and every
   referenced extent, matching host layout, full-pool CRC and the planned
   free-run geometry with two valid roots. Then type a known factory
   translation, a personal override, a multi-stroke entry, and a previously
   tombstoned outline that should fall back to a lower-priority dictionary.
   Record actual output and confirm a known one-stroke hint on the right half.
5. **Repeat on a disposable pool for interruption qualification.** Capture
   the command output and cut power once during erasure, once during transfer,
   and once during publication or immediately afterward. Use separate
   attempts. Record which phase you actually hit. A missing reply calls for
   reconnect and inspection, never automatic erase. If the board matches
   the optimized target, an explicit repeat verifies and skips erase.
   An unprovisioned board can receive a deliberate retry. A partial root
   publication can leave READY with a mismatched full-pool CRC. Stop there.
   To recover the original layout on the spare in that case, independently
   verify `original.json` and explicitly authorize the separate disaster
   recovery command:

   ```sh
   python3 tools/storage/native_recovery.py /path/to/original.json \
     --restore --serial SPARE_USB_SERIAL --confirm-erase SPARE_USB_SERIAL
   ```

   Reboot and compare the restored spare against the original backup before
   any new write test. Reflashing a UF2 alone does not reset the QSPI pool.

## Tonight's hardware checkpoint and test sequence

Firmware is frozen at `b9eba3fc63b3820faca00bb0834b26b7c3461dd5`. Use the
matching experimental left UF2 below and keep the current right firmware.
The host harness verifies the downloaded bytes. Firmware capabilities and
the USB serial are checked separately. The running firmware does not attest
its exact build hash over HID.

| Spare left | Boot volume | SHA-256 |
| --- | --- | --- |
| [Boardsource](https://github.com/disarmyouwitha/StenoSquid/actions/runs/44644245484/artifacts/14587853339) | `BS_KBD` | `de6d5289c47e3430441a8ec0815d7cf8fdd2c5436c9ec41a094ab3c2b4056f9d` |
| [nice!nano](https://github.com/disarmyouwitha/StenoSquid/actions/runs/44644245484/artifacts/14587883347) | `NICENANO` | `4725d274bba60b80d7417a960745bbab1afff95e393b392a19e818e99b6c1496` |

1. **Verify the artifact and spare.** In the repository, select the downloaded
   `zmk.uf2` and run these host-only commands. Flash the spare left yourself if
   it needs the checkpoint. Flashing the controller leaves the external pool in
   place. Disconnect the everyday board and close Studio and Javelin before
   opening wired HID.

   ```sh
   python3 -m pip install -r requirements.txt
   UF2='/path/to/downloaded/zmk.uf2'
   python3 tools/bench/gate_b.py verify-uf2 --uf2 "$UF2"
   ```

   These commands read the USB serial and Native state. Set the serial to the
   exact value displayed and confirm it is the spare.

   ```sh
   python3 tools/bench/gate_b.py identify
   SERIAL='YOUR_SPARE_USB_SERIAL'
   python3 stenosquid.py --status --serial "$SERIAL"
   ```

   Expect `NATIVE2 READY`. A blank or incompatible disposable pool needs
   separate provisioning. Confirm the physical spare and permission to erase
   its external dictionaries before using this **pool erase** command.

   ```sh
   python3 stenosquid.py --install --confirm-erase --serial "$SERIAL"
   ```

   Recheck status. Use an empty personal journal above enabled Lapwing Base.
   Keep any valued pool disconnected. Reflashing the left is never a pool reset.
2. **Check the stock typing path.** With a blank editor focused, verify
   ordinary keys from each half, Javelin `KAT` → “cat”, and `A/ROUPBD` →
   “around” with the display hint `ARPBD` when hints are enabled. Repeat over
   BLE with USB unplugged and both halves powered. Complete a WORDS Practice
   round and check the editor is empty. Reconnect USB and close the editor
   connection before the next step.
3. **Capture and verify a read-only baseline.** Use a new output directory and
   run idle. The backup takes a read session. Keep it separate from typing
   latency measurements. The runner saves the catalog, capabilities, status
   and counters, known forward hits and a miss, the flash map, a verified
   version 2 bundle, and per-dictionary JSON exports and SHA-256 hashes.

   ```sh
   OUT="build/gate-b-$(date -u +%Y%m%dT%H%M%SZ)"
   python3 tools/bench/gate_b.py baseline --serial "$SERIAL" --uf2 "$UF2" --out "$OUT"
   python3 tools/storage/native_recovery.py "$OUT/before-bundle.json"
   ```

   Expect `$OUT/baseline.json` with `result: pass`, a verified backup, and an
   empty personal export. A deferred map is recorded as deferred. Retry the
   read-only map in Manager while idle. Keep this folder and bundle privately.
4. **Convert the personal fixture.** This **writes external flash** and checks
   the prior catalog, exact personal export, and saved backup before modifying
   the spare. Run only after confirming the exact serial. The runner previews
   allocation, saves the seed, then stages the small fill fixture with the
   actual journal state. After reboot it verifies the same ID, order, flags
   and host layout. `STKPWHR` must keep “sqcheck preserved” and `KAT/TA` must
   resolve to “sqcheck catalog.”

   ```sh
   python3 tools/bench/gate_b.py convert --serial "$SERIAL" --confirm-write "$SERIAL" --out "$OUT"
   ```
5. **Edit, check priority, and consolidate.** Both commands **write external
   flash** and run separately. The edit replaces `KAT/TA`, adds `KAT/KAT`,
   and tombstones personal `PWOB`. The readback checks exact exports and the
   factory fallback for `PWOB`. Between the two commands, try `KAT/TA` and
   `KAT/KAT` on the keys over USB and BLE while the edits still live in the
   overlay. Optionally use Manager to put personal below Lapwing Base and
   then disable it. Confirm that factory `KAT` wins. Restore the original
   order and selection before consolidation. Those Manager changes also write the catalog.

   ```sh
   python3 tools/bench/gate_b.py edit --serial "$SERIAL" --confirm-write "$SERIAL" --out "$OUT"
   python3 tools/bench/gate_b.py consolidate --serial "$SERIAL" --confirm-write "$SERIAL" --out "$OUT"
   ```

   Expect unchanged catalog identity and order, byte-identical effective
   exports after edits and consolidation, and zero live personal overlay
   records after consolidation.
6. **Check map, throughput, cold boot, and wake.** Follow the
   [physical checklist](#physical-gate-b-checklist). With no transfer active,
   remove USB and battery power, turn the spare back on, then run this
   **read-only** persistence check. It compares saved data and selections
   after the operator's power cycle. It cannot observe the cycle itself.
   Repeat the read-only checks after at least 16 minutes asleep on battery
   and a left-key wake. Test BLE reconnect before reconnecting USB.

   ```sh
   python3 tools/bench/gate_b.py cold --serial "$SERIAL" --out "$OUT"
   python3 tools/bench/gate_b.py inspect --serial "$SERIAL" --out "$OUT"
   ```

   The repeatable `inspect` command also records the map, catalog, counters,
   exact forward probes and personal export without approving any write.
7. **Optional controlled interruption.** On the same disposable spare, first
   take a new lean bundle and use the existing named fixture so the committed
   result is easy to distinguish. The following commands only read the pool
   or produce a host file. Preview opens and closes a staging session
   without writing.

   ```sh
   python3 stenosquid.py --backup-all "$OUT/consolidated-bundle.json" --serial "$SERIAL"
   python3 tools/storage/native_recovery.py "$OUT/consolidated-bundle.json"
   PYTHONPATH=tools/bench python3 - "$OUT/cut-source.json" <<'PY'
   import json
   import pathlib
   import sys
   from snapshot_feasibility import format_stroke, synthetic
   entries = {"/".join(map(format_stroke, outline)): text.decode() for outline, text in synthetic(50000).items()}
   pathlib.Path(sys.argv[1]).write_text(json.dumps(entries))
   PY
   python3 -u tools/storage/native_snapshot.py "$OUT/cut-source.json" --name SQCut --format snapshot --serial "$SERIAL"
   ```

   If the preview fits, repeat its final command with `--stage` **to write
   external flash**. After an early `Staged N/M` line with `N < M`, remove USB
   and battery power. Record the last message and the actual cut cue.
   Reconnect and run read-only `inspect` before any retry. `SQCut` must be
   absent or export all 50,000 translations. If present, use the **read-only**
   command below and compare the JSON with `cut-source.json`. The personal
   export and previously enabled dictionaries must still match.

   ```sh
   python3 stenosquid.py --backup "$OUT/cut-export.json" --dictionary SQCut --serial "$SERIAL"
   ```
8. **Qualify USB rescue separately.** Use a second expendable spare with the
   frozen checkpoint, the first spare disconnected, and a fresh verified
   lean bundle. Follow the [serial-confirmed pool-erase procedure](#usb-recovery-on-a-spare).
   Test a blank pool and separately cut during erase, programming, and
   verification. After a reported success, cold-boot and capture a read-only
   baseline to compare all exported JSON and hashes, IDs, flags, order and
   host layout with the source. Each capture needs a different `--out`.
   An offline repack simulation image is outside this procedure.

**Stop safely.** Keep the console transcript, latest stage or failure JSON,
verified bundle, exports and map. If a reply is lost, a stage fails, the pool
reports read-only, or data differs, leave the board powered and use
read-only `inspect` before any retry. A failed write may have committed in
part or in full. The runner blocks a retry in that evidence folder. It never
reprovisions or starts USB recovery. Before publication, the power-cut
expectation is the previous complete catalog. After a complete publication,
verify the new catalog and exact exports. Return to a known snapshot-free
pool before going back to stable firmware.

### Automated 200 WPM replay

No manual speed target is needed. Prepare the pinned data and replay the
passage on the host:

```sh
python3 tools/dictionaries/prepare.py
python3 tools/bench/run_typing_replay.py /path/to/west-workspace/javelin-steno --out build/typing-replay
```

The runner checks the Javelin revision from `config/west.yml` and builds its
real engine with the Native overlay adapter. It reads the actual compiled
JSC4 maps and journal from a simulated NOR image. The corpus contains two
everyday passages totaling 36 words, multi-stroke outlines, a compact personal
override, a journal replacement, a tombstone with factory fallback, and a
deliberate miss followed by undo. Independent lookup probes confirm the
translation and winning dictionary before and after a Native checkpoint.
The replay checks the resulting editor text after **every stroke**, including
intermediate output, backspaces, punctuation and the final output.

Rate is based on the words in the finished passage. Each full passage cycle
also contains its actual number of strokes, including a deliberate miss and
undo. Those consume chord slots without adding any words. At each target,
the runner spaces strokes uniformly over the word-derived duration. It
holds 100, 150 and 200 WPM for at least 60 seconds per target, then plays
300 WPM bursts with at least 20 seconds of active replay and two-second gaps
between passage cycles. The achieved burst rate excludes those planned gaps. Use `--virtual` for a fast one-cycle
correctness check at each target. That mode advances the engine clock without
sleeping and does not report achieved wall-clock WPM.

`report.json` records the exact corpus hash, phase counts, achieved WPM,
mismatches and p50/p95/p99/max for `StenoEngine::Process`. The host uses an
absolute monotonic deadline for each stroke and separately reports when it
actually entered the engine. Engine samples exclude tape parsing, pacing
sleep, validation, and sample output. `corpus.json`, `probes.tsv`, `tape.tsv`,
and `samples.tsv` preserve the inputs and raw timing. Firmware and device
memory are unaffected. The simulated pool exists for the native harness.

This exercises host translation and captured key events. Physical scanning,
split synchronization, USB/BLE delivery, QSPI timing, firmware scheduling and
maintenance contention need separate evidence. The frozen firmware has no
per-stroke profiler. The older `run_dictionary_benchmark.py` diagnostic
samples a different build and transport. The replay reuses its percentile
calculation, and keeps diagnostic device measurements separate.

### Physical Gate B checklist

Use a comfortable pace. For each run, record board, transport, expected and
actual output, and any disconnect, dropped stroke, replay or stall.

1. Type normal keys from both halves over USB and unplugged BLE. Try
   `KAT`, a two-stroke translation, a Practice round, and an enabled hint.
2. On the spare, edit and consolidate using the earlier fixture steps.
   Exercise the overlay hit before consolidation, then compare exports and
   check the dictionary ID, order and enabled state afterward.
3. Type during a Manager layout scan and during USB idle maintenance. Check
   the map completes or defers cleanly. Bracket the session with read-only
   `inspect` reports for `auto_checkpoints`, `auto_reclaimed`, `work_steps`,
   `input_overflows` and `max_replay_delay_ms`. If maintenance did not run,
   leave that part pending. Keep full Verify and explicit imports separate.
4. Test sleep, left-key wake, USB disconnect, BLE reconnect and split
   reconnect. Record the first correct stroke after wake. Check persisted
   contents after a deliberate cold reboot with the read-only `cold` stage.
5. Exercise a controlled publication interruption on the test spare. Separately
   qualify the serial-confirmed USB rescue path on expendable hardware and
   compare the cold-boot export, dictionary settings and host layout.

### Firmware and hardware qualification

CI runs the same compact snapshot, recovery, and exact-byte capacity tests for
the default Native firmware. Physical Gate B verified Boardsource USB and BLE
typing, Practice, battery sleep/wake, a cut during staging, and cold readback of
the committed 100,000-entry fixture. USB recovery from a partial transfer
matched the full backup on a nice!nano spare. Physical timing during an
in-flight catalog-root write and demanding on-device typing rates remain open.

### HID editor capability audit

| Editor operation | Journal-only store and lean snapshots |
| --- | --- |
| Active Dictionaries | List, enable, disable and toggle over USB/BLE HID. Host layout get/set is also allowed when built in. |
| Personal entry edit and view | Add/remove personal edits and read the effective personal JSON. A personal snapshot routes the edits into its journal overlay. Manager can clear the personal dictionary over USB. |
| Forward lookup | `lookup_stroke` returns the winning translation, including overlays and priority. |
| Arbitrary snapshot or named edits, export and lifecycle | Manager and the project CLI use Native commands over USB. The standard HID allowlist only exposes `print_dictionary user_dictionary`, so Manager reads selected dictionary extents through the Native backup session. |
| Exact reverse, text prefix and partial outline | `lookup`, `lookup_prefix` and `lookup_partial_outline` were already outside the HID allowlist before the indexed experiment and return `ERR Command unavailable on this keyboard` on both transports. Native still uses journal search internally, and SHI1 gives typing hints. |

The transport error is explicit. How the unmodified Javelin website presents
it remains a browser check on hardware. Manager does not call these commands.
The previous test notes assumed the generic Javelin console bindings were
reachable through HID. The allowlist and its tests show that they were not.

### Recovery readiness

The recovery CLI reconstructs and validates the complete bundle before USB
is opened. It requires the exact serial twice and rechecks the connected
device before erasure and after reconnecting. The device verifies the staged
image before either root is committed. The host always checks the full physical pool CRC after reconnect, then
checks the recovered catalog and host layout. A mismatched checksum stops the
attempt without an automatic second erase. An explicit retry on a still
matching raw pool skips erase. Later maintenance can change the raw checksum
while leaving exported contents equivalent, so inspect before choosing a
retry.

Version 2 USB rescue restores the bundle's recorded layout. The optimized
bundle uses the separate [offline repack and spare recovery](#offline-repacking)
procedure. Physical qualification requires first install, intentional
interruptions, cold readback and typing on the spare.
