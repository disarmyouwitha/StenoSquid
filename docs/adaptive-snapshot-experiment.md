# Adaptive dictionary snapshots experiment

This branch adds opt-in named snapshots and explicit personal conversion over USB. Existing journal dictionaries continue to open in their original format. Keep using the known-good Native release on your everyday board.

Start with the [hardware checkpoint and test sequence](#tonights-hardware-checkpoint-and-test-sequence) for the corrected build, fixture commands, and the preserved `4e1f9a4` rollback.

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

## Hardware smoke test

1. Use a spare left half or a disposable QSPI fixture with the ordinary Native dictionaries installed. Record the USB serial number, save a standard `stenosquid.py --backup-all` before introducing any snapshot rows, and keep the known-good left UF2 handy. Keep the matching right firmware running.
2. Use the matching Actions artifact, `Boardsource-Left-Snapshot-Experimental` or `NiceNano-Left-Snapshot-Experimental`. Each archive contains its own `zmk.uf2`. The new left can open an existing unmarked Native pool. Confirm normal typing and `KAT` before staging anything.
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
5. Use `stenosquid.py --backup smoke-export.json --dictionary 'Snapshot Smoke'` to export the effective named dictionary. Once a snapshot row exists, `stenosquid.py --backup-all experiment.json` creates a self-contained recovery bundle. Verify it offline with `python3 tools/storage/native_recovery.py experiment.json`.

## Complete experimental backups

Version 2 records the selected binary catalog, the checked host layout, and the exact referenced extents with a SHA-256 checksum. This preserves compact snapshot and pinned image contents, pinned source digests, logical IDs, priority, checkboxes, and every live override and tombstone. It includes raw journal bytes, which can contain old translations that no longer appear in a dictionary export. Treat it as private. The CLI publishes the file only after validation and flush.

Offline reconstruction verifies catalog ownership, compact lookup parity, journal records and both indexes, then produces two valid roots in memory. It needs no downloads. The version 1 backup and restore format remains available for ordinary journal-backed stores. Manager and the ordinary installer refuse version 2 recovery.

## Snapshot lifecycle

Rename and export work through Manager and the standard dictionary commands. Manager can also delete a snapshot after confirmation. The experimental CLI provides read-only planning for the following operations. Append `--stage` only after checking the proposed operation and retaining a backup.

```sh
python3 tools/storage/native_snapshot.py new-baseline.json --name 'Snapshot Smoke' --serial YOUR_USB_SERIAL --replace
python3 tools/storage/native_snapshot.py --name 'Snapshot Smoke' --serial YOUR_USB_SERIAL --consolidate
python3 tools/storage/native_snapshot.py --name 'Snapshot Smoke' --serial YOUR_USB_SERIAL --delete
```

Replacement changes the compact baseline and keeps the existing override and tombstone records. Consolidation takes the current effective dictionary as the new baseline, then removes only that dictionary's live overlay references. Both keep its ID, position and enabled state. An empty effective dictionary cannot be consolidated into JSC4. Delete retires the dictionary and its compact extent without reusing the ID. Ordinary journal maintenance can later reclaim its stale edits. The allocator protects both roots, retained readers and the three-segment reserve. A capacity refusal leaves the previous catalog in place.

The on-flash catalog magic remains `SQBCAT02`. Byte 11 of a user-owned row is the version-one snapshot marker. It references a single-member JSC4 extent and shares the existing ID-qualified overlay. Factory rows and existing records retain their original format. Stable firmware rejects a marked root and may see the preceding root or enter recovery. Before staging a snapshot, rollback is simply reflashing the stable left. After staging, return to an untouched spare pool or restore the pre-test backup using the existing standard Native provisioning path on your disposable fixture. That explicit operation rewrites the pool. Preserve the backup separately and keep the experimental firmware installed until you are ready for that restore.

Bluetooth editing is follow-up work.

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

On the experimental firmware, Manager previews named imports using both host planners and shows the bytes, added segments, staging cost, current free run and any refusals. Automatic prefers a compact image if it saves steady-state segments and either at least 1 MiB or at least one segment of logical bytes and 25% of journal bytes. A compact plan also needs space for a same-size replacement under the current layout. The Advanced selector offers Journal or Compact snapshot when eligible. The approved plan is recomputed before publication. Source changes, appended edits, changed catalogs or different occupied segments invalidate the approval. Leave the name blank to keep the existing personal merge path.

## Read-only flash map

Choose **Scan layout** in Manager while the keyboard is idle and awake. The grid shows all 256 segments, highlights extents referenced by both roots, and distinguishes older-root-only journals and indexes from current ownership. Hover over a segment to see both roots' descriptors. Unreferenced space comes from the catalog geometry. The view does not verify flash contents or scan journal records for stale entries. The three reserve segments are deducted from total staging capacity and are not pinned to fixed addresses.

The endpoint has no polling, input pause, staging lease or write path. It defers when flash is asleep, the lock is busy, input is pending, or its short metadata-read budget expires. A missing or unreadable root in recovery makes free space unknown. The physical timing and typing behavior of this button still need a board check.

## Hardware acceptance checklist

1. Complete the [initial smoke test](#hardware-smoke-test) on the matching spare left. Keep the existing right firmware. In normal typing and Javelin, check both halves and one multi-stroke translation.
2. Save a version 2 backup after creating the compact dictionary and adding the three sample overlay edits. Verify it offline and export the named dictionary. Check that `KAT` is the replacement, `TAS` is present, `PWOB` is absent, and the untouched multi-stroke entry is present.
3. Rename the snapshot in Manager, move it below an enabled factory dictionary, and toggle it off and back on. Check lookup priority, name and flags after a cold boot. Move it back above the factory dictionary for the remaining checks.
4. Use `--replace` with `{"KAT":"second baseline cat","PWOB":"second baseline Bob","TEFT":"snapshot test"}`. Check that the original override still wins, the tombstone still hides `PWOB`, and `TEFT` appears. Export the result, run `--consolidate`, and export again. The two dictionaries should match. Click **Measure saved edits** and confirm the snapshot's overlay count is now zero. Replace, consolidate, and rename should leave the numeric ID unchanged.
5. Scan the layout before and after staging or deleting the test snapshot. Inspect the snapshot extent and free-run count. Request another scan while typing and check whether the request defers or completes without delaying text. On the named import form, preview Automatic and both Advanced choices before staging. Upload a different same-size file and check that the old approval does not carry over.
6. For a **test-only power cut on the spare**, use a larger throwaway named import or same-ID replacement to allow time to interrupt staging. With a verified host bundle and an exported copy of the test dictionary safely on disk, disconnect USB and switch off its battery during data staging, then reconnect. Record whether the old or complete new catalog appears and compare the effective export and priority. Repeat near the catalog publish message if the window is observable. For ambiguous acknowledgment, reconnect and inspect the ID and generation before retrying. Do not infer physical cut coverage from console progress alone.
7. Delete the disposable snapshot through Manager. Confirm its row disappears and an unrelated dictionary and its edits survive reboot. Check that wired setup, normal Javelin WebHID, typing and Practice still work.

Full Gate B still needs physical cutoff timing, dense-store and sustained high-speed typing measurements, plus USB recovery on a blank or partly written pool. Existing named journal conversion and Bluetooth editing are outside this build.

## Personal conversion

Run the experimental CLI with the USB serial of the spare left.

```sh
python3 tools/storage/native_snapshot.py --convert-personal --serial YOUR_USB_SERIAL
python3 tools/storage/native_snapshot.py additional.json --convert-personal --serial YOUR_USB_SERIAL
```

Append `--stage` after reviewing the contiguous extent requirement and the total staging segments. The second form fills missing outlines from Plover JSON. Local sets and tombstones win. The same catalog row keeps its ID, name, priority and enabled flag. Existing sets become the compact baseline and existing tombstones stay in the shared overlay. Further adds, replacements and removals use that overlay. Use `--consolidate --name user_dictionary` to fold subsequent edits into a new baseline. The bulk clear command remains unavailable for compact personal dictionaries.

The [personal capacity report](benchmarks/personal-snapshot-capacity.json) validates every translation for Plover and the legal 200,000-entry target. A nearly full journal may leave too little contiguous room to preserve the old root while staging. The full Plover journal fixture left 29 contiguous segments and was refused. Both large compact images fit on the pinned factory pool using fill-only conversion, with no clean install. A fixture with 158 free segments but a seven-segment largest run refused both images. A small real Native transaction recovered across 92 simulated flash boundaries with two tear lengths, preserving the disabled flag, priority, ID and tombstone.

## Lean compact storage and search capabilities

Commit `48f6f97` made `compile_snapshot()` append SQS1 by default when it added
exhaustive editor search. Named import preview and staging, personal conversion,
same-ID replacement, and consolidation all call that compiler. The offline
repacker subsequently inherited the same default when converting journals.
Installing or restoring an existing image kept the exact captured bytes.

New snapshots use the original JSC4 plus SHI1 bytes. The existing SQS1 reader
and version 2 validation still support indexed snapshots. They remain indexed
until an explicit replacement or consolidation. Offline repacking copies
existing compact images as-is apart from relocating absolute pointers. It
compiles any newly converted dictionaries as lean JSC4.

| Path | What it supports |
| --- | --- |
| Javelin compact map | Exact forward lookup with per-outline-length hash tables, occupancy bitmaps and 24-bit rows, plus entry enumeration for export. Upstream partial-outline search enumerates rows of eligible lengths. Upstream exact reverse and prefix methods consume candidate entry addresses supplied by a separate reverse-map layer. The plain forward tables cannot produce these candidates efficiently by text. |
| StenoSquid SHI1 | An exact translation can suggest one preferred single chord. The compiler selects the candidate with the fewest keys, then the smallest mask. There is no multi-stroke candidate, prefix enumeration or list of all equivalent single chords. The Native adapter checks that the selected outline still resolves to this dictionary and is visible through overlays. |
| Native journal and overlays | Forward and reverse checkpoint indexes with a bounded live tail preserve current sets, replacements and tombstones. The reverse index accelerates exact text for current journal sets. Prefix and partial-outline queries enumerate live journal entries, retaining their existing result limits. |
| Lean StenoSquid compact baseline | Forward lookup, Practice, export, and SHI1 hints. The USB editor can return the one-stroke SHI1 candidate for exact text, plus existing journal matches. Multi-stroke exact reverse, prefix and partial-outline matches from the lean baseline are absent. A missing search result does not prove an outline is absent. |
| Existing SQS1 compact baseline | The editor still searches indexed exact text, prefixes and partial outlines with overlay, enabled-state and priority filtering. Typing-time hints stay on SHI1. |
| USB host | Export a dictionary to search all of its effective translations and outline sequences. Export merges that dictionary's baseline with its own overlays. Use the saved catalog order and enabled flags to reconstruct cross-dictionary winners. For any future on-device scan, a result cap alone cannot bound no-hit latency. A scan would need a fixed work budget, continuation and cancellation that yields to typing. No such scan is enabled. |

The StenoSquid Native adapter reads compact data through QSPI-safe accessors.
It does not call the generic Javelin compact-map partial scan against these
snapshots. The upstream source audit used the pinned Javelin implementation.
[Compact map](https://github.com/disarmyouwitha/javelin-steno/blob/de5a629bb383581882dfd6a5b2b5b9b44b71dc9c/dictionary/compact_map_dictionary.cc)
· [Reverse-map layer](https://github.com/disarmyouwitha/javelin-steno/blob/de5a629bb383581882dfd6a5b2b5b9b44b71dc9c/dictionary/reverse_map_dictionary.cc)

### Capacity gate

| Same input | Original JSC4 + SHI1 | Regressed SQS1 | Corrected default | Conversion / consolidation staging |
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
· [Historical SQS1 breakdown](benchmarks/compact-index-overhead.json)

## Experimental USB recovery on a spare

Verify the bundle offline with `python3 tools/storage/native_recovery.py experiment.json` first. On an expendable spare running the matching experimental firmware, close the editor, connect over USB, and confirm its serial. Recovery erases the spare's shared dictionary pool when it differs from the bundle. It leaves the reserved final segment outside the transfer. Use the same serial twice so a mismatched target cannot start.

```sh
python3 tools/storage/native_recovery.py experiment.json --restore \
  --serial SPARE_USB_SERIAL --confirm-erase SPARE_USB_SERIAL
```

The host validates every extent before contacting the board. The firmware accepts the recovery transport without a mounted Native catalog. It erases the pool, writes and checks the content with the catalog commit words still erased, then publishes each root and verifies the complete image. After reboot, the host verifies dictionary IDs, order and flags before applying and reading back the saved host layout. If the final acknowledgment is lost, a matching image can finish without re-erasing. If the store remains incomplete, reconnect and explicitly repeat with the same bundle. No destructive retry is automatic.

For physical qualification, test an initially blank spare and interruptions during **Erasing**, data transfer, and the **Verifying** message. Record the point reached, reboot with USB and battery restored, and repeat the command. Verify the exported test dictionary and overlays, dictionary ordering, checkboxes, and host layout against the bundle. Inspect the physical catalog after a near-publication cut if the timing is observable. Host fault injection covered 138 boundaries in two initial states and with two tear lengths. Neither Manager nor automatic import selection offers destructive recovery.

### Import eligibility

The preview distinguishes a same-size copy-on-write replacement, an initial
image that fits but would need more space for a future replacement, and an
initial image that cannot stage. The second case remains gated. Experimental
USB recovery restores a verified bundle exactly. Manager offers no destructive
fallback. See the checklist below for the expected editor search differences.

### Offline repacking

The repacker takes a verified version 2 bundle and plans a compact physical layout on the computer. Existing compact images keep their contents and relative search indexes. The planner relocates their absolute JSC4 pointers, regenerates the journal and both checkpoint indexes, and keeps three free segments in reserve. It considers journal-only user dictionaries in catalog order and converts each only if it saves packed segments. It leaves that dictionary's tombstones and all other dictionaries' live edits in the shared overlay. Unsupported outlines and candidates that would save no space are reported.

A journal-only store can be captured as a complete bundle with `--backup-all --bundle`. This reads flash and writes a host file. It holds the existing backup lease, so run it while idle. The repack step itself stays offline.

```sh
python3 stenosquid.py --backup-all build/source-bundle.json --bundle --serial SPARE_USB_SERIAL
python3 tools/storage/native_repack.py build/source-bundle.json --report build/repack-report.json
```

Add `--image build/repack-simulation.bin` for a raw image used by fake-NOR tests. The planner emits no USB deployment manifest or reinstallable bundle. Use new output paths. `--convert NAME` can narrow the candidates and `--keep-representations` packs the existing formats.

For the full 147,424-entry Plover personal journal, the offline target shrank from **226 to 111 used segments**. The contiguous free run grew from **29 to 144 segments**. All 438,827 per-dictionary translations and 243,093 active priority winners matched. Repacking an already lean 200,000-entry personal snapshot kept its 105 used segments and verified its contents without changing formats. The mixed fake-NOR fixture verifies preservation of a disabled personal row, a pinned override and tombstone, a relocated existing snapshot, reverse/prefix/partial lookup, and 16 selected cuts and retries. Physical USB recovery and deployment of rebuilt layouts still require qualification. [Repack capacity report](benchmarks/offline-repack-capacity.json)

## Tonight's hardware checkpoint and test sequence

Use the two `Left-Snapshot-Experimental` artifacts from the corrective CI run and use the scripts from the same branch revision. The compiler runs on the host, so replacing a UF2 alone leaves existing snapshot bytes unchanged. Keep the matching right firmware in place. The following `4e1f9a4` artifacts are the rollback/reference UF2s. The kit's [JSON fixtures](fixtures/adaptive-snapshot/) assume a disposable, provisioned spare with an empty journal-only personal dictionary and Lapwing Base enabled. If a spare has other personal data, export it before the test and compare fixture entries as a subset of that export instead of wiping it.

| Left half | Boot volume | SHA-256 |
| --- | --- | --- |
| [Boardsource experimental UF2](https://github.com/disarmyouwitha/StenoSquid/actions/runs/44631422014/artifacts/14582765781) | `BS_KBD` | `70e8c2535f5ad8bb76c103040f21da4bd29350582210487764c305edb9100316` |
| [nice!nano experimental UF2](https://github.com/disarmyouwitha/StenoSquid/actions/runs/44631422014/artifacts/14581836442) | `NICENANO` | `5ecf110179a78ef045d3e9351ed6aae6aff7b557a31d3d06ca326aecda338ad9` |

| Effect | Actions |
| --- | --- |
| **Read-only external flash** | Catalog and lookup queries, export, full backup, import preview without `--stage`, scanning the map, local bundle verification and offline repack. Backups and some previews hold the catalog for a consistent read. Run them idle. |
| **Writes external flash without a pool erase** | Seeding personal entries, conversion with `--stage`, editing and removing outlines, changing dictionary priority or checkboxes, staging a named fixture, deleting it, and consolidation. |
| **Controller firmware or settings** | Copying a UF2 programs the controller. Normal typing uses the active keyboard mode. Practice updates controller settings when its results are saved. |
| **Can erase the external dictionary pool** | The experimental USB recovery command and the standard installer or full restore when invoked with erase confirmation. Run these only on the separately designated expendable spare. |

1. **Establish the baseline.** Use the existing split pair. Verify ordinary keys from each half over USB. In Javelin, stroke `KAT` for “cat”, then `A/ROUPBD` for “around”. With display hints enabled and the pinned Lapwing entries active, the latter has the one-stroke alternative `ARPBD`. Unplug USB and repeat normal typing and Javelin over a paired Bluetooth profile. Complete one WORDS Practice round and confirm there is no text in the focused editor. Record the mode, profile, and displayed hint you actually see.
2. **Capture before changing anything.** Connect the spare by USB and close other editor sessions. Run the following from the repo. Replace the example serial with the spare's exact HID serial. If only the spare is connected, the discovery command below returns it.

   ```sh
   PYTHONPATH=tools/dictionaries python3 -c 'from import_personal import open_console; exec("with open_console() as c: print(c.serial)")'
   SERIAL='YOUR_SPARE_USB_SERIAL'
   F=docs/fixtures/adaptive-snapshot
   OUT=$(mktemp -d)
   python3 stenosquid.py --list-dictionaries --serial "$SERIAL" > "$OUT/before-catalog.json"
   python3 stenosquid.py --backup-all "$OUT/before-bundle.json" --bundle --serial "$SERIAL"
   python3 tools/storage/native_recovery.py "$OUT/before-bundle.json"
   ```

   Keep that recovery bundle and the known-good left UF2 outside your test outputs. For a journal-only store, a second `--backup-all` without `--bundle` saves the ordinary backup format for standard Native rollback.
3. **Convert and inspect personal.** These commands seed a journal entry, preview conversion without writing, then stage the conversion after you have reviewed the free run. The existing `STKPWHR` translation must win over the colliding fill entry. Check the same personal ID, row order, and checkbox against the saved catalog. For these lookups, keep personal enabled above overlapping factory dictionaries.

   ```sh
   python3 stenosquid.py --import "$F/seed-journal.json" --serial "$SERIAL"
   python3 tools/storage/native_snapshot.py "$F/fill-personal.json" --convert-personal --serial "$SERIAL"
   python3 tools/storage/native_snapshot.py "$F/fill-personal.json" --convert-personal --serial "$SERIAL" --stage
   python3 stenosquid.py --list-dictionaries --serial "$SERIAL"
   ```

   Forward lookup must resolve `KAT/TA` through the lean baseline. In the Javelin console, `lookup sqcheck cat` can offer the SHI1 candidate `KAT`. The baseline-only multi-stroke result for `lookup sqcheck catalog`, and the lean baseline results for `lookup_prefix 20 sqcheck ca` and `lookup_partial_outline KAT 20`, are absent by design. These entries must still appear in the full personal export. Temporarily put personal below enabled Lapwing Base and check that the factory `KAT` wins and the personal hint disappears. Reorder it back, disable personal, repeat, then re-enable it. Those priority and checkbox changes write the catalog.

   If the spare already has a known indexed snapshot, record its baseline-only exact, prefix and partial results before changing it and repeat them on the corrected firmware. If you choose to consolidate that indexed snapshot, forward translations and export must match and the new baseline uses lean search behavior.
4. **Edit and consolidate.** Set `PERSONAL_ID` to the current numeric ID. The edit file replaces `KAT/TA` and adds `KAT/KAT`; the explicit remove adds a tombstone for `PWOB`. The old exact reverse hit for “sqcheck catalog” and the personal “sqcheck removed” hit should disappear. Pinned fallback for that outline can still resolve. Export before and after consolidation, compare the JSON as dictionaries, and confirm that the personal ID is stable and its overlay count is zero in Manager's **Measure saved edits**.

   ```sh
   PERSONAL_ID='NUMERIC_PERSONAL_ID'
   python3 tools/storage/native_manage.py --serial "$SERIAL" --dictionary "$PERSONAL_ID" --import "$F/edit-personal.json"
   python3 tools/storage/native_manage.py --serial "$SERIAL" --dictionary "$PERSONAL_ID" --remove PWOB
   python3 stenosquid.py --backup "$OUT/edited.json" --dictionary user_dictionary --serial "$SERIAL"
   python3 tools/storage/native_snapshot.py --consolidate --name user_dictionary --serial "$SERIAL" --stage
   python3 stenosquid.py --backup "$OUT/consolidated.json" --dictionary user_dictionary --serial "$SERIAL"
   python3 - "$F/expected-after.json" "$OUT/edited.json" "$OUT/consolidated.json" <<'PY'
   import json
   import pathlib
   import sys
   expected, edited, consolidated = (json.loads(pathlib.Path(p).read_text()) for p in sys.argv[1:])
   assert edited == consolidated
   assert all(edited.get(outline) == text for outline, text in expected.items())
   assert "PWOB" not in edited
   print("Exports match, expected fixture entries present and PWOB absent")
   PY
   ```
5. **Map, cold boot and wake.** In Manager, check **Scan layout** while idle. Start a second scan, immediately refocus a blank editor, and type several chords. Confirm the map either loads or defers, with no lost, repeated, or delayed chords. Record any observable delay. Save or photograph the free run and snapshot color. With no transaction underway, remove USB and battery power from the left, reconnect, and verify the same export, order and checkboxes. For sleep, leave the left on battery untouched for 16 minutes. Wake with a left key, give the split time to reconnect, then type from both halves and repeat a lookup. Repeat one Practice and hint check with the snapshot present.
6. **Interrupt one ordinary copy-on-write update.** On the same disposable fixture, first save the consolidated bundle and export. Use a throwaway named dictionary so a mixed result is obvious. Generate this reproducible 50,000-entry import file and run a preview before staging.

   ```sh
   python3 stenosquid.py --backup-all "$OUT/consolidated-bundle.json" --bundle --serial "$SERIAL"
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

   If preview fits, repeat with `--stage`. During the data-transfer window after an early `Staged N/M` line with `N < M`, remove USB and turn the left battery off. Reconnect and inspect before retrying. `SQCut` must be entirely absent or have all 50,000 translations. If it is present, export it with `--backup --dictionary SQCut` and compare it with `cut-source.json` as JSON dictionaries. The personal export must still match. Retry if needed after inspecting the catalog. Delete the throwaway test dictionary through Manager after checking it. A second cut near publication is optional. Record the actual cue because the console message does not prove the physical instant of a root write.
7. **Qualify USB rescue separately.** Use a second expendable left with the checkpoint firmware, ideally with a blank or partially provisioned pool. Check its exact serial with the first spare disconnected. Use a verified bundle from the consolidated fixture, never a repack simulation image. Follow the [serial-confirmed recovery procedure](#experimental-usb-recovery-on-a-spare). Cut during erase, programming, and verification on separate attempts, then repeat explicitly with the same bundle. Check complete exports, IDs, order, enabled states, and the saved host layout after cold boot. No staged repack output is part of this test.

**Rollback.** Before a staged write, aborting or disconnecting leaves the selected catalog intact. After a copy-on-write cut, reconnect on the same checkpoint firmware, inspect status and exports, and keep the original bundle intact. A catalog with snapshot rows needs compatible experimental firmware. Reflashing the preserved UF2 alone keeps the external pool as it is. If the expendable test pool must be replaced, use a verified pre-test bundle and the explicitly confirmed experimental recovery path. Return to stable firmware only with an ordinary, snapshot-free pool. Leave any ambiguous or read-only catalog available for inspection before choosing an erase.


### Physical 200 WPM test

Use a provisioned spare with Lapwing Base active, first before and then after
adding the lean snapshot and representative overlays. Keep the right half
connected and include chords using both hands. Repeat over USB and over BLE
with the left USB cable unplugged.

- Pace the five-word line “the cat is the cat” every **1.50 seconds** for
  60 seconds using `-T/KAT/S/-T/KAT`. Count **40 lines**, or 200 intended words.
  Then pace 20 lines every **1.00 second** for a 300 WPM burst. Add a separate
  pass alternating those outlines with `A/ROUPBD`, overlay hits, and misses.
  Record the actual achieved pace and compare the full editor output with
  the expected text, allowing normal capitalization and implicit spaces.
- Repeat during a read-only map scan and while an existing maintenance job is
  active. Test deliberate import/Verify separately with a throwaway fixture
  and record its blocking window and what happens to strokes entered during it.
  Watch for lost, reordered or replayed chords, pauses and split disconnects.
- For end-to-end latency, record chord releases and host output on one
  timebase. Use a synchronized instrumented trace if available. Slow-motion
  video can flag visible stalls but has limited resolution. Save the samples
  for p50, p95, p99 and worst-case calculations. Report console round-trip
  times separately from typing and separately from firmware-internal lookup
  time. Capture first correct key/chord and split-ready latency after cold
  power-up and 16 minutes asleep.
- Capture the CI linker RAM/flash allocation and, if a separately qualified
  diagnostic build is available, runtime stack/heap high-water marks. The
  checkpoint firmware has no per-stroke lookup timing or runtime stack
  telemetry for this test. Keep those measurements pending when there is no
  suitable capture. Compare the same cadence and setup with the rollback
  reference to separate board and transport effects from dictionary changes.
