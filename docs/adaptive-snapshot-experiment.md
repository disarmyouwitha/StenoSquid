# Adaptive dictionary snapshots experiment

This branch adds an opt-in, USB-only experiment for independent named compact dictionaries. The personal dictionary and existing journal dictionaries keep their representation. Keep using the known-good Native release on your everyday board.

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

The strict NOR stage test cuts writes at 57 flash boundaries with two tear lengths and checks recovery, dictionary IDs/order/selections and unrelated entries. The firmware also checks staged extent CRCs and uses the normal Native dictionary adapter for forward lookup. Full compact-baseline reverse lookups, prefix/partial search, the post-churn benchmark matrix, and hardware latency and power-cut acceptance remain open. Gate A host timings do not predict typing latency.

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

Offline reconstruction verifies catalog ownership, compact lookup parity, journal records and both indexes, then produces two valid roots in memory. It needs no downloads. The version 1 backup and restore format remains available for ordinary journal-backed stores. USB installation of a version 2 bundle or a reconstructed image is disabled pending physical recovery qualification.

## Snapshot lifecycle

Rename and export work through Manager and the standard dictionary commands. Manager can also delete a snapshot after confirmation. The experimental CLI provides read-only planning for the following operations. Append `--stage` only after checking the proposed operation and retaining a backup.

```sh
python3 tools/storage/native_snapshot.py new-baseline.json --name 'Snapshot Smoke' --serial YOUR_USB_SERIAL --replace
python3 tools/storage/native_snapshot.py --name 'Snapshot Smoke' --serial YOUR_USB_SERIAL --consolidate
python3 tools/storage/native_snapshot.py --name 'Snapshot Smoke' --serial YOUR_USB_SERIAL --delete
```

Replacement changes the compact baseline and keeps the existing override and tombstone records. Consolidation takes the current effective dictionary as the new baseline, then removes only that dictionary's live overlay references. Both keep its ID, position and enabled state. An empty effective dictionary cannot be consolidated into JSC4. Delete retires the dictionary and its compact extent without reusing the ID. Ordinary journal maintenance can later reclaim its stale edits. The allocator protects both roots, retained readers and the three-segment reserve. A capacity refusal leaves the previous catalog in place.

The on-flash catalog magic remains `SQBCAT02`. Byte 11 of a user-owned row is the version-one snapshot marker. It references a single-member JSC4 extent and shares the existing ID-qualified overlay. Factory rows and existing records retain their original format. Stable firmware rejects a marked root and may see the preceding root or enter recovery. Before staging a snapshot, rollback is simply reflashing the stable left. After staging, return to an untouched spare pool or restore the pre-test backup using the existing standard Native provisioning path on your disposable fixture. That explicit operation rewrites the pool. Preserve the backup separately and keep the experimental firmware installed until you are ready for that restore.

Bluetooth editing and destructive USB snapshot rescue are follow-up work.

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

Full Gate B still needs physical cutoff timing, dense-store and sustained high-speed typing measurements, and verified USB recovery from a blank or partly written pool. Compact-baseline reverse/prefix/partial search, compact conversion of existing personal/journal dictionaries, and Bluetooth editing are outside this build.

## Personal conversion

Run the experimental CLI with the USB serial of the spare left.

```sh
python3 tools/storage/native_snapshot.py --convert-personal --serial YOUR_USB_SERIAL
python3 tools/storage/native_snapshot.py additional.json --convert-personal --serial YOUR_USB_SERIAL
```

Append `--stage` after reviewing the contiguous extent requirement and the total staging segments. The second form fills missing outlines from Plover JSON. Local sets and tombstones win. The same catalog row keeps its ID, name, priority and enabled flag. Existing sets become the compact baseline and existing tombstones stay in the shared overlay. Further adds, replacements and removals use that overlay. Use `--consolidate --name user_dictionary` to fold subsequent edits into a new baseline. The bulk clear command remains unavailable for compact personal dictionaries.

The [personal capacity report](benchmarks/personal-snapshot-capacity.json) validates every translation for Plover and the legal 200,000-entry target. A nearly full journal may leave too little contiguous room to preserve the old root while staging. The full Plover journal fixture left 29 contiguous segments and was refused. Both large compact images fit on the pinned factory pool using fill-only conversion, with no clean install. A fixture with 158 free segments but a seven-segment largest run refused both images. A small real Native transaction recovered across 92 simulated flash boundaries with two tear lengths, preserving the disabled flag, priority, ID and tombstone.
