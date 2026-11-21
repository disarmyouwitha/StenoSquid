# Native dictionary storage

Follow the [project installation guide](../README.md#how-to-install) for
matching firmware and Manager. See [connection support](bluetooth.md) for
operations available over USB and Bluetooth.

## Dictionaries and editing

- Lapwing Base, its four extras, and Plover Base are separate factory images.
  The installer lets you select which to install. You can add or remove each
  later in Manager. Each has its own lookup priority and enabled checkbox.
- **Import JSON** creates a compact dictionary when you give it a name. Clear
  the suggested filename to import into the personal dictionary's journal.
- Edits to compiled dictionaries go into a shared journal. A local deletion
  hides the outline in that dictionary, and a lower-priority enabled dictionary
  can still supply it. **Export JSON** reads the effective dictionary with its
  baseline and edits combined.
- **Compact** in Dictionaries previews conversion of a journal-only dictionary
  or consolidation of a compact dictionary with edits. It folds the effective
  entries into a new compact baseline, preserving the ID, priority, and enabled
  state. Future edits go into the journal. Factory dictionaries keep their
  pinned baselines. **Delete** on the personal dictionary clears its contents.
- Compact JSC4 + SHI1 supports forward translation, Practice, and one-stroke
  hints. For exhaustive text or outline searches, export the dictionary and
  search on your computer.

## Memory and capacity

The 16 MiB external flash is divided into 64 KiB blocks. Two retained catalog
roots protect the current and previous layouts. The last block is reserved
for diagnostics, and staged operations leave three free blocks for maintenance.

**Scan Memory** shows which dictionaries and storage structures occupy each
block. **Compact Memory** previews reclaimable journal blocks, and **Reclaim**
applies the cleanup. **Preview USB Repack** shows how a rebuilt pool would
change the layout. It can grow the largest contiguous free run even when the
total free space is similar. The preview runs before the complete backup or any
pool erase. If you confirm **Repack**, Manager saves and verifies the backup
before rebuilding the pool.

The capacity test requires byte-for-byte equality with the original Javelin
compiler for the same input. Figures below cover the compiled image. A
copy-on-write update additionally needs staging space and may need room for
journal and index changes.

| Corpus | Image bytes | 64 KiB blocks |
| --- | ---: | ---: |
| Plover fixture, 147,424 entries | 2,339,624 | 36 |
| Synthetic fixture, 200,000 entries | 1,931,932 | 30 |

[Capacity measurements](benchmarks/lean-snapshot-capacity.json) ·
[Offline repack measurements](benchmarks/offline-repack-capacity.json)

## Backup and USB recovery

**Download full backup** saves a self-contained version 2 bundle with the
catalog, dictionary IDs, priority, enabled states, pinned images, overlays,
tombstones, and host layout. Keep it private because journal bytes can contain
older translations. Use JSON exports when you want portable dictionary data.

Verify a bundle offline from the repository root.

```sh
python3 tools/storage/native_recovery.py /path/to/stenosquid-backup.json
```

If Manager offers **Restore original backup** after an interrupted repack,
reconnect the left half over USB and use that backup. For manual recovery,
identify the exact left-half USB serial with `python3 tools/bench/gate_b.py identify`
and verify the bundle first. The following command requires the same target
serial twice and can erase the entire external dictionary pool.

```sh
python3 tools/storage/native_recovery.py /path/to/stenosquid-backup.json \
  --restore --serial TARGET_USB_SERIAL --confirm-erase TARGET_USB_SERIAL
```

After recovery, power-cycle the keyboard and verify the catalog, settings and
known translations. Flashing a UF2 leaves the external dictionary pool in
place. Full recovery restores the bundle's saved layout. Offline repack produces
a separate, optimized bundle. See the [offline repack and guarded USB
procedures](archive/adaptive-snapshot-gate-a-b.md#offline-repacking) for the CLI
and readback checks.

## Validation

The [Native development record](archive/native-engine-development.md) and
[Gate A and Gate B results](archive/adaptive-snapshot-gate-a-b.md#firmware-and-hardware-qualification)
record storage and physical tests. These include Boardsource USB and BLE
typing, Practice, sleep/wake, an interrupted staged write, a 100,000-entry
cold readback, and USB recovery on nice!nano. Host replay checks translation
correctness at modeled rates up to 300 WPM. Physical scanning, split and BLE
latency at sustained 200 WPM, and a physical cut during a catalog-root write
remain separate validation work.
