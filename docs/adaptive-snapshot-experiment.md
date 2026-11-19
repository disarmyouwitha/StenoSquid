# Adaptive dictionary snapshots experiment

This branch adds an opt-in, USB-only experiment for one new named compact dictionary at a time. The user dictionary and existing named dictionaries keep their journal representation. Automatic format selection, in-place optimization, snapshot deletion and same-ID replacement are pending. Keep using the known-good Native release on your everyday board.

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
   python3 tools/storage/native_snapshot.py smoke.json --name 'Snapshot Smoke' --serial YOUR_USB_SERIAL
   ```

   A minimal fixture is `{"KAT":"snapshot cat","KAT/TA":"snapshot multi","PWOB":"snapshot Bob"}`. Append `--stage` to publish. Reconnect after reboot and check that the named row and its enabled state appear in Active Dictionaries. If a final acknowledgment is lost, reconnect and check the catalog before retrying. Reusing the same name is refused.
4. Check the new translations, then add an outline, replace a baseline translation and remove another from the named dictionary. Get its numeric ID with `stenosquid.py --list-dictionaries --serial YOUR_USB_SERIAL`. For the sample above, save `{"TAS":"snapshot added","KAT":"snapshot replacement"}` in `overlay.json`, then run:

   ```sh
   python3 tools/storage/native_manage.py --serial YOUR_USB_SERIAL --dictionary ID --import overlay.json
   python3 tools/storage/native_manage.py --serial YOUR_USB_SERIAL --dictionary ID --remove PWOB
   ```

   The new outline and replacement should win, and the removal should fall through to the next enabled dictionary. Move or toggle the dictionary to exercise priority, then cold boot and repeat the checks. After a test-only interrupted transfer, reboot and confirm that either the old catalog or the complete new catalog is visible.
5. Use `stenosquid.py --backup smoke-export.json --dictionary 'Snapshot Smoke'` to export the effective named dictionary, including its overlay. The experimental host code refuses whole-store backup once a compact user row exists because the current whole-store restore format cannot reconstruct its baseline.

The on-flash catalog magic remains `SQBCAT02`. Byte 11 of a user-owned row is the version-one snapshot marker. It references a single-member JSC4 extent and shares the existing ID-qualified overlay. Factory rows and existing records retain their original format. Stable firmware rejects a marked root and may see the preceding root or enter recovery. Before staging a snapshot, rollback is simply reflashing the stable left. After staging, return to an untouched spare pool or restore the pre-test backup using the existing standard Native provisioning path on your disposable fixture. That explicit operation rewrites the pool. Preserve the backup separately and keep the experimental firmware installed until you are ready for that restore.

The read-only memory map, automatically selected formats, Bluetooth editing and destructive USB snapshot rescue are follow-up work.
