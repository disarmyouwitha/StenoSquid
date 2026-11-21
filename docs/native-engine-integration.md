# Native dictionary storage

Native is the default in [v0.1.2](https://github.com/disarmyouwitha/StenoSquid/releases/tag/v0.1.2)
for both Boardsource and nice!nano left firmware.
For installation, upgrades, and the Manager, use the [project README](../README.md#how-to-install).

## How the store works

- The 16 MiB external flash uses 64 KiB segments. Two catalog generations track
  shared ownership of compiled bases, journals, and indexes. The final segment
  is reserved for diagnostics. **External flash map** in Manager shows the current layout.
- Lapwing, its four extras, and Plover each have a separate compact image.
  Each dictionary keeps an independent ID, enabled state, and position in lookup
  order. A local edit or removal applies to the selected dictionary. Lower-priority
  enabled dictionaries can still supply an outline hidden in a higher one.
- The Manager runs locally over USB HID. It edits, imports, exports, renames, and
  reorders dictionaries and applies pinned base updates. Updates stage and verify
  the new image before publishing it. Local edits take precedence and conflicts
  appear in the update report.
- Full backups capture the ordered catalog, IDs, pinned source hashes, local
  edits and removals, and host layout. Restore rebuilds the pinned bases and
  explicitly reprovisions the target pool. For older fixed-layout firmware, export
  a personal JSON backup before flashing and follow the README upgrade steps.
- After 64 pending edits, automatic compaction can run on USB power while the
  keyboard is idle. One pass can move up to 4 KiB of live records from older
  journals. Resumed activity cancels unpublished work. An already published
  catalog finishes recovery. **Compact now** runs a larger USB-assisted pass.

## What v0.1.2 verified

| Area | Evidence |
| --- | --- |
| Translation and catalog | Pinned-source lookup checks, independent extras, duplicate-outline priority, persistent selections, and local override/reset behavior. |
| Manager | Hardware tests for named import, edit, export, rename, reorder, and delete. A packed Lapwing Commands export matched all 146 expected entries. |
| Pinned base updates | A temporary Lapwing UK Additions change passed on Boardsource. The local override won, the conflict report showed both values, and reset exposed the upstream entry. Rollback preserved the original catalog settings and entries. |
| Automatic compaction | The corrected Boardsource build cleared 71 edits, freed three journal segments (192 KiB), and preserved all 73 test translations. The longest observed slice was 80.5 ms while idle. |
| Recovery and capacity | Real power cuts during import staging and just before catalog commit preserved the live store. A sparse near-full test verified refusal and the three-segment reserve. Host fault injection covers compaction interruptions and recovery export. |
| Full backup and restore | A real backup rebuilt and verified offline. Host tests exercised restore and confirmed a matching catalog, local edits, and pinned bases. |
| Release | CI passed and all eight public UF2 downloads were verified. [Release build](https://github.com/disarmyouwitha/StenoSquid/actions/runs/44565319621) |

The [development record](archive/native-engine-development.md) preserves the full
test results and earlier experiments. The [storage decision](dictionary-storage-decision.md)
and [benchmark report](dictionary-benchmark-report.md) explain the format choice.
The [original spec](archive/unified-dictionary-storage-spec.md) contains the full
acceptance matrix.

## Dense-store hardware follow-up

On the installed Boardsource Left build, 6,900 temporary entries with
1,120-byte translations filled 201 of 253 allocatable segments (79%). After
110 edits, automatic compaction freed four 64 KiB segments and all 6,900
translations matched on readback. The host planner refused an additional
oversized import before sending erase or write commands.

All 1,630 paced USB HID lookups returned the expected answers. Round-trip p95
was 25.2 ms in the first dense window and 18.2 ms in the final window, versus
4.4 ms in the sparse idle baseline. The worst USB round trip was 160.9 ms and
the longest firmware worker slice was 309.2 ms. These measurements cover the
dictionary console path. Physical Bluetooth typing latency remains open.

Mac-side USB resets restored editor access after two large-store reboots.
The temporary dictionary was removed, 122 stale journals were reclaimed in
cleanup, and a full integrity scan passed. The original catalog, entries, and
host layout matched the pre-test backup.

## Remaining validation

1. **Typing and Bluetooth during compaction.** Record end-to-end output and
   latency at sustained 200 WPM and during bursts, including when typing resumes
   during automatic work. Check for dropped or reordered chords
   and delayed bursts. The idle slice measurement covers flash work on the board.
2. **Full restore onto a spare board.** Install a real backup and check named
   and personal entries, base overrides and removals, priority, selections, and
   host layout after reboot. Exercise interruption and retry during provisioning.
3. **Power loss during compaction.** Cut power during journal and index writes
   and around catalog publication. After reboot, verify the original or fully
   committed state and check the retained catalog and allocator.
4. **Near-full wear and runtime headroom.** Repeat imports and edits at higher
   occupancy and over a longer period. Measure heap and stack headroom and check
   wear across repeated compaction. Entry capacity depends on outlines,
   translation lengths, and staging headroom.

Larger 32 and 64 MiB chips have a separate hardware, addressing, and format
qualification. The current release supports the 16 MiB layout.
