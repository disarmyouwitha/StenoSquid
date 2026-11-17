# Personal dictionary: constant-time writes in one 10 MiB region

Status: **draft for implementation.** Written against commit `7908a68`.
Not built or run; the design is from reading `user_dictionary_snapshot.h` and
`user_dictionary_store.cc`. Timings are estimates until measured on hardware.

This revision replaces the earlier two-bank draft. There are no banks.

## 1. Problems

1. **Every edit rewrites everything.** The dictionary is a sorted snapshot, so
   `Store::Save` rewrites all entries into the other slot to add, change or
   delete one. At 90,000 entries that is minutes under the engine lock.
2. **Half the space is a spare copy.** Two 4 MiB slots hold one dictionary, and
   2 MiB above them is unused. Capacity is about 90,000 entries on a flash that
   has 10 MiB free.

## 2. Design in one page

The 10 MiB above the base dictionaries becomes one pool of **64 KiB segments**.
Each segment is one of:

| Kind | Holds |
|---|---|
| **Record** | Dictionary entries, appended one after another. This is the truth. |
| **Table** | Part of a hash table: 4-byte slots, each written once, pointing at records. |
| **Meta** | A tiny log saying which table segments are current. |
| free | Nothing. |

Three facts make it work:

- **NOR flash can clear bits without an erase.** An empty slot is `0xFFFFFFFF`;
  claiming it is one 4-byte program. Deleting a record clears one bit in its header.
  So add, edit and delete never move existing data.
- **Records are self-describing.** Each has its own sizes, flags and CRC. The
  tables and the meta log can always be rebuilt from the record segments, so
  they are caches, not single points of failure. This replaces the "second full
  copy" as the safety net.
- **Space is reclaimed a segment at a time.** When free segments run low, the
  record segment with the most dead bytes has its live records re-appended at
  the head and is erased. That needs a couple of spare segments, not a spare
  dictionary.

| Operation | Flash work |
|---|---|
| Add | append 1 record, program 2 slots |
| Edit | add the new record, clear 1 bit in the old |
| Delete | clear 1 bit |
| Lookup | read a few adjacent slots, then 1 record |
| Every ~2,300 adds | one 64 KiB erase to open a new record segment |
| Background | segment cleaning, in small slices while idle |
| Rare | rebuild one hash table (when it fills with stale slots or must grow) |

**Capacity:** about 190,000 typical entries (§3.6), up from about 90,000.

**Clean break:** no migration. Old-format data is treated as unformatted. The
owner re-imports.

### 2.1 Relationship to upstream Javelin

Upstream Javelin (`dictionary/user_dictionary.cc` at the pinned commit
`2272667`, the code behind jthlim's own boards) uses the same core idea: one
region split into a forward hash table (1/8), a reverse hash table (1/8) and an
append-only data block (3/4), with 4-byte slots where `0xFFFFFFFF` is empty.
This design keeps that idea and deliberately differs in four places. Do not
"simplify" back to upstream:

| | Upstream | This spec | Why |
|---|---|---|---|
| Replacing or deleting a slot | Overwrites the slot in place. Because flash bits cannot be set without an erase, that reads a 4 KiB block into RAM, erases it and rewrites it. | Slots are never rewritten; the record carries a `DEAD` bit. | Power loss during upstream's rewrite loses 1,024 slots, and nothing rebuilds them. |
| Reclaiming space | None. The data block only grows; when full, adds fail until the whole dictionary is reset. | Segment cleaning (§6). | A dictionary that is edited for years must not need a wipe. |
| Integrity | No per-entry checksum; tables cannot be rebuilt. | CRC per record; tables and meta are rebuildable from records (§8). | This is what replaces the second full copy. |
| Table size | Fixed at 1/4 of the region. | Sized to the entry count, rebuilt to grow. | Small dictionaries stay cheap; large ones get the space. |

Upstream's entry header is 4 bytes against 8 here (the extra word is the CRC),
so upstream stores about 14 % more typical entries in the same data space.
That is the price of the checksum and is accepted.

## 3. On-flash format

All integers little-endian.

### 3.1 Region

| | |
|---|---|
| Start | flash offset `0x600000` (unchanged `SLOT_START`; base dictionaries stay below it) |
| Segments | 159 × 64 KiB, numbered 0..158, ending at `0xFF0000` |
| Above | `0xFF0000`–`0xFFFFFF` left unused (the existing code already avoids the last sector) |

Segment boundaries line up with the flash's 64 KiB block erase.

### 3.2 Segment header (first 16 bytes of every segment)

| Field | Type | Meaning |
|---|---|---|
| `magic` | u32 | `0x3355445a` ("ZDU3"). `0x00000000` = retired. `0xFFFFFFFF` = erased. |
| `kind` | u8 | 1 record, 2 forward table, 3 reverse table, 4 meta |
| `index` | u8 | table segments: position within the table. Others: 0. |
| `epoch` | u16 | table segments: which build of that table. Others: 0. |
| `sequence` | u32 | counts up by one for every segment ever allocated; never reused |
| `crc` | u32 | CRC-32 of the preceding 12 bytes |

A segment is **free** if its header is anything other than valid: erased,
retired, or corrupt. Free segments are always block-erased when allocated, so
their previous contents never matter.

**Retiring** a segment = programming its `magic` to `0x00000000`. This is done
*before* erasing it, so a half-finished erase can never bring dead records back
to life.

### 3.3 Record (in record segments, from offset 16)

```
word 0   flags and sizes
word 1   CRC-32, seeded with the segment's sequence number, over strokes then text
         stroke_count x u32 strokes
         text bytes, zero-padded to a multiple of 4
```

Word 0:

| Bits | Field | Notes |
|---|---|---|
| 31 | `ALLOC` | 0 = this space is taken |
| 30 | `COMMIT` | 0 = record is complete |
| 29 | `DEAD` | 0 = deleted, replaced or moved |
| 28..24 | `stroke_count - 1` | 1..16 strokes |
| 23..12 | `text_bytes` | 0..2047 |
| 11..0 | `check` | low 12 bits of FNV-1a over the bytes of (`stroke_count`, `text_bytes`) |

Flags are 1 when erased and are cleared in order by programming word 0 again
with one more bit cleared. **Live** = `ALLOC=0, COMMIT=0, DEAD=1`.

Records are 4-byte aligned and never cross a segment boundary. A segment's
records end at the first word 0 that reads `0xFFFFFFFF`, or when fewer than 8
bytes remain. Size: `8 + 4*stroke_count + round_up_4(text_bytes)`.

Seeding the CRC with the segment sequence means bytes left from any earlier
use of that flash can never pass as a current record.

### 3.4 Hash tables

Two independent tables: **forward** (key = outline) and **reverse** (key =
translation text). Each is 1..32 table segments. A table segment holds 16,380
slots after its header, so a table has `S = segments × 16,380` slots.

Slot (4 bytes):

| Bits | Meaning |
|---|---|
| 31..22 | `tag` = bits 31..22 of the key's hash |
| 21..14 | record's segment number (0..158) |
| 13..0 | record's offset within the segment / 4 |

`0xFFFFFFFF` = empty. A slot is never rewritten.

- Hash: the existing FNV-1a (`hash_bytes`) over the stroke words (forward) or
  the text bytes (reverse).
- Home slot = `hash % S`. Collisions: linear probing, wrapping at `S`.
- Slot *i* lives in the table segment whose `index` is `i / 16380`, at byte
  `16 + 4 × (i % 16380)`.

**Stale slots are harmless by construction.** A slot may point at a record that
is dead, or into a segment that has since been erased and reused. A lookup
accepts a slot only if the target has a valid header `check`, is live, has
exactly the key being looked up, and passes its CRC. If all of that holds, the
target *is* the one live record for that key, however the slot came to point
there. Otherwise the slot is skipped.

### 3.5 Meta log (in the one meta segment, from offset 16)

Fixed 80-byte **state records**, appended:

| Field | Type | Meaning |
|---|---|---|
| `flags` | u32 | `ALLOC` / `COMMIT` bits as in §3.3; rest 1 |
| `void_before` | u32 | record segments with a lower `sequence` are not part of the dictionary (used by reset and import) |
| `fwd_epoch`, `fwd_segments` | u16, u16 | current forward table build and its segment count |
| `rev_epoch`, `rev_segments` | u16, u16 | same for reverse |
| `max_strokes` | u32 | upper bound on the longest live outline |
| `reserved` | 56 bytes | zero |
| `crc` | u32 | CRC-32 of the preceding 76 bytes with `flags` taken as 0 |

The **last committed, CRC-good state record in the highest-sequence valid meta
segment** is the current state. Appending one is the commit point for a table
switch, a reset or an import. Table segments are found by scanning headers for
the matching `kind` and `epoch`; the state record does not list them.

When the meta segment is full (819 records — it will take years), allocate a
new meta segment, write the current state as its first record, then retire the old one.

### 3.6 Capacity

Typical entry: 28-byte record. At 190,000 entries:

| Use | Segments |
|---|---|
| Records (5.1 MiB) | 82 |
| Two tables at 73 % load | 32 |
| Room to rebuild one table | 16 |
| Meta, plus one while rolling over | 2 |
| Cleaning reserve | 3 |
| **Total** | **135 of 159** |

The 24 segments of slack absorb dead records between cleanings and
longer-than-typical entries. The hard limit is "no free segment after
cleaning", not an entry count.

## 4. State kept in RAM

About 1.5 KB, no dictionary-sized allocation:

- per segment: kind, sequence, epoch/index, dead bytes (records only);
- current state record; segment lists of both tables in index order;
- head record segment and its tail offset; next sequence number;
- counters: live entries, slots used per table, integrity errors;
- flags: `stats_ready`, `rebuilding`, `needs_recovery`.

## 5. Operations

All run under the engine lock. "Program" is the existing verified write
(program, read back, compare). Reads use the memory-mapped window through the
existing `xip_load` helpers; `QspiDevice` already handles switching modes for writes.

### 5.1 Allocating a segment

1. Pick any free segment. Prefer the one with the lowest erase wear if that is
   tracked; otherwise the lowest number.
2. Block-erase it (64 KiB erase).
3. Program its header with the next sequence number.

Three free segments are held back as the **cleaning reserve**; only cleaning
and meta rollover may use them.

### 5.2 Lookup by outline

1. `h = hash(strokes)`; `i = h % S`.
2. Read slot `i`. Empty → not found.
3. If the tag matches, check the target as in §3.4. Accept → found.
4. `i = (i + 1) % S`; repeat, at most `S` times.

Reverse lookup is the same on the reverse table, but it continues to the end of
the probe run and returns every accepted record (several outlines can share one
translation), dropping duplicates of the same record.

A record that fails its CRC is skipped and counted in `integrity_errors`.

### 5.3 Add or replace `(outline, text)`

1. Look up the outline. Live record with identical text → done, no write.
2. If a table is rebuilding → reply "busy".
3. If the record does not fit in the head segment, allocate a new head (§5.1).
   If no segment is free outside the reserve: run cleaning (§6) in the
   foreground until one is; if none can be freed → "dictionary full".
4. Program word 0 with `ALLOC=0` and the sizes.
5. Program word 1, strokes, text.
6. Program word 0 with `COMMIT=0`. **← commit point**
7. If step 1 found an older live record: clear its `DEAD` bit.
8. Program the first empty slot in the forward probe run.
9. Program the first empty slot in the reverse probe run.
10. If either probe in 8–9 passed more than 256 slots, or a table is over 87 %
    used, schedule a rebuild of that table (§7).
11. If `stroke_count > max_strokes`: append a state record with the new bound
    **before** step 4 (so the bound is never too low).

### 5.4 Delete `outline`

Look up; if found, clear the record's `DEAD` bit. **← commit point.** Add its
size to the segment's dead bytes.

### 5.5 Reset

Append a state record with `void_before = next sequence` and fresh empty
tables (allocate one segment each first, with new epochs). **← commit point.**
Then retire every record segment older than that and the old table segments.

### 5.6 Open

Runs on the engine-init work item, never on the key thread (cleanup spec P3).

1. Read all 159 segment headers. Next sequence = highest seen + 1.
2. Find the current state record (§3.5).
   - No valid meta segment and no valid record segment → **format**: allocate
     meta, one forward and one reverse table segment, one record segment; write
     the first state record.
   - No usable state but record segments exist → **recovery** (§8).
3. Collect the current table segments. Any table segment with another epoch,
   any record segment older than `void_before`, and any extra meta segment is
   stale: retire it. If a current table is missing a segment → recovery for that table.
4. The head is the highest-sequence record segment. Walk it to find the tail.
   - `ALLOC=0, COMMIT=1`, valid `check`: an interrupted add. Skip it.
   - Invalid `check`: the header word was torn. Treat the rest of the segment
     as unusable: the head is closed and the next add allocates a new one.
5. **Replay the last committed record** `R` (in the head, or in the previous
   record segment if the head is empty). Writes are serialised, so only this
   record can be half-linked. Make each of these true if it is not already:
   - no other live record has `R`'s outline (clear that record's `DEAD` bit);
   - if `R` is live, the forward table reaches `R`;
   - if `R` is live, the reverse table reaches `R`.
6. The dictionary is now usable for lookups and writes.
7. **Background statistics walk**, in slices at low priority: visit every
   record segment once to compute live count and dead bytes per segment. Set
   `stats_ready`. Until then, status reports the count as unknown and cleaning
   picks the oldest record segment instead of the emptiest.

Steps 1–5 cost 159 small reads plus at most two segment walks, whatever the
dictionary size.

### 5.7 Power-fail behaviour

| Power lost… | At next open |
|---|---|
| before the commit point of an add | Uncommitted record, skipped. Old translation still live. |
| after the commit point, before the slots | Replay finishes the job. New translation live. |
| during a delete | The bit cleared or it did not. |
| during a segment allocation | Segment has no valid header → still free. |
| during cleaning | Every moved record was committed, linked and its original marked dead one at a time; the victim is simply cleaned again. |
| while erasing a victim | It was retired first → free. |
| during a table rebuild | New table segments carry an epoch no state record names → retired at open. Old table still current. |
| during reset or import | Before the state record commits: old dictionary intact. After: new one. |

Every operation is all-or-nothing and the old value survives until the new one
is committed.

## 6. Segment cleaning

**When:** in the background whenever fewer than 8 segments are free and some
record segment has dead bytes; in the foreground only when an add finds nothing
free outside the reserve.

**Victim:** the non-head record segment with the most dead bytes. Never clean a
segment with none.

**Steps**, a slice of about 32 records at a time, releasing the engine lock
between slices:

1. For each live record in the victim: verify its CRC (if bad: count it, clear
   its `DEAD` bit, move on). Otherwise perform §5.3 steps 3–9 with the same
   outline and text — the "older live record" in step 7 is the original in the victim.
2. When no live record remains: retire the victim, then block-erase it.

Each moved record uses one new slot in each table, which is why tables
eventually need rebuilding.

## 7. Table rebuild

One table at a time, so only one table's worth of extra space is ever needed.

**When:** the table is over 87 % used (live + stale slots), a probe ran past
256 slots, a table segment is missing or corrupt, or by command.

**Size:** the largest of these that fits in free segments outside the reserve:
live count ÷ (16,380 × 0.5) segments, else ÷ 0.75. If even that does not fit,
clean first; if it still does not fit → "dictionary full" (lookups keep working
on the old table).

**Steps:**

1. Set `rebuilding`. Adds, edits and deletes reply "busy" until done. Lookups
   keep using the old table.
2. Allocate the new table's segments with `epoch + 1`.
3. Walk every record segment (skipping those older than `void_before`), in
   slices, releasing the engine lock between them. For each live record,
   program the first empty slot in its probe run in the new table.
4. Append a state record naming the new epoch and segment count. **← commit point**
5. Retire and erase the old table's segments. Clear `rebuilding`.

Cost is one small program per live entry — the slots land at scattered
positions and cannot be batched without dictionary-sized RAM. Expect seconds
for a few thousand entries and several minutes near 190,000 at the current
2 MHz flash clock. Show the existing upload/busy animation while it runs.

## 8. Recovery

If the meta log or a table is unusable but record segments exist, nothing is
lost: write a fresh state record (`void_before = 0`, `max_strokes = 16`) and
rebuild both tables from the records (§7). While rebuilding, any outline that
appears in more than one live record keeps the copy in the higher-sequence
segment (later offset within a segment) and the others get `DEAD` cleared.

This is the answer to "what if the index is damaged": the entries themselves
are still there and still individually checksummed.

## 9. Console and host tool

Command names used by the Javelin browser tool stay the same.

| Command | Behaviour |
|---|---|
| `add_user_entry`, `remove_user_entry`, `reset_user_dictionary`, `lookup_stroke` | As now, implemented by §5. May reply "busy" during a table rebuild. |
| `print_dictionary` | Walks record segments in sequence order and prints live records. Order is insertion order, not sorted. Releases the engine lock between segments. |
| `user_dictionary_status` | `version` 3, `entries` (or `counting`), `free_segments`, `record_segments`, `dead_bytes`, `fwd_slots_used` / `fwd_slots`, `rev_…`, `integrity_errors`, `state` (`ready` / `cleaning` / `rebuilding` / `recovering`), `next_sequence`. |
| `user_dictionary_verify` (new) | Check every record's CRC and that every live record is reachable from both tables. Print counts. Read-only. |
| `user_dictionary_rebuild` (new) | Force a rebuild of both tables. |
| `user_dictionary_import` | Rewritten, below. |

### 9.1 Bulk import

The host builds finished segments — record segments and both tables — and the
keyboard writes them into free segments. Nothing changes until the final state
record, so an import is atomic **when there is room for it beside the current
dictionary**.

- `import begin <record_segments> <fwd_segments> <rev_segments> [replace]` →
  firmware replies with the sequence number the first uploaded segment must
  carry and the epochs to use. Without `replace`, it refuses if there are not
  enough free segments. With `replace`, it first performs a reset (§5.5) so the
  whole region is available.
- `import segment <n>` + 512-byte data chunks → firmware allocates a segment,
  programs it sequentially, then checks its header and, for record segments,
  walks it checking every record CRC.
- `import finish` → firmware checks that every live record is reachable through
  both uploaded tables and no outline appears twice, then appends the state
  record (`void_before` = first uploaded sequence, new epochs). **← commit point.**
  Old segments are retired afterwards. Any failure retires the uploaded segments.

`replace` is not atomic: if it is interrupted, the keyboard's dictionary is
empty or partial until the import is re-run. That is acceptable only because
the host holds the complete copy — see the backup rule below.

### 9.2 `stenosquid.py`

- `--backup FILE.json` (new): export the personal dictionary to a file.
  Document it prominently in the README as the way to protect a dictionary.
- `--import FILE.json`:
  1. Export the current dictionary and merge (the file wins), as today.
  2. If fewer than ~500 entries differ: send individual `add_user_entry`
     commands and stop.
  3. Otherwise build the segments with `tools/dictionaries/segment_image.py`
     (new; implements §3 exactly) and upload.
  4. If the keyboard says there is not enough free space, write the merged
     dictionary to a timestamped backup file next to the input, tell the user,
     and retry with `replace`.

## 10. Code changes

| File | Change |
|---|---|
| `src/steno_squid/user_dictionary_snapshot.h` | Replace with the new store; rename to `user_dictionary_log.h`. Header-only, pointer-free, no Zephyr includes, so host tests compile it. Keep `Device` (`Read` / `Program` / `Erase`), add `EraseBlock64K`. Keep `crc32`, `hash_bytes`, `WriteVerified`. Delete `Source`, `Store::Save`, `PageWriter::Text`, the sorted-index readers, `ReverseStart`, `Validate`, the old import. |
| `src/steno_squid/user_dictionary_store.cc` | `QspiUserDictionary` calls the new store. Delete `EditSource` and the save path. Add the new commands, the background work item (statistics walk, cleaning, rebuild slices), and `EraseBlock64K` on `QspiDevice` (`NRF_QSPI_ERASE_LEN_64KB`). Replace the `static_assert` on `SLOT_START + 2 * SLOT_BYTES` with one on `0x600000 + 159 × 64 KiB <= 0xFF0000`. |
| `src/steno_squid/javelin_web_hid.cc`, `engine_init.cc` | Add the new command names to the console whitelist (or the single table from cleanup spec S6). |
| `tools/dictionaries/import_personal.py` | New status keys, new import protocol, small-change path, `segment_image.py`. |
| `stenosquid.py` | `--backup`; backup-before-replace. |
| `tools/tests/test_user_dictionary_snapshot.cc`, `tools/dictionaries/test_import_personal.py` | Rewrite (§11). |
| `README.md` | Capacity line (~190,000), "External Memory Map" text, and regenerate `assets/qspi-flash-map.png`. |

Unchanged: base dictionary layout below `0x600000`, the engine-facing
`StenoDictionary` interface, limits of 16 strokes and 2,047 text bytes.

## 11. Tests (host, against a fake NOR device)

The fake `Device` must enforce NOR rules — a program may only clear bits (fail
the test if it would set one); erases set a sector or block to `0xFF` — and
count programs and erases per segment.

1. **Model check.** Hundreds of thousands of random add / edit / delete /
   lookup / reverse-lookup operations against the store and a `std::map`, in a
   deliberately small region (say 12 segments) so cleaning and rebuilds happen
   constantly. Results agree at every step and across reopen.
2. **Power-fail injection.** For every operation type — including cleaning,
   rebuild, reset, meta rollover and import — cut power after each single
   device call in turn, and also mid-call (apply a random subset of a program's
   bits; leave an erase half done with random bytes). Reopen. The dictionary
   equals the state before or after the operation, and the next write succeeds.
3. **Constant cost.** With 190,000 entries loaded: an add that does not open a
   new segment is at most 6 programs and 0 erases; a delete is 1 program; open
   (steps 1–5) performs a number of reads that does not grow with entry count.
4. **Stale slots.** Fill, delete, clean and reuse segments so that old slots
   point into new data; no lookup may ever return a wrong or dead entry.
5. **Resurrection.** Half-erase a retired segment full of dead records; none reappear.
6. **Recovery.** Destroy the meta segment; destroy a table segment; corrupt the
   last state record. Every live entry is still found after open.
7. **Capacity.** 190,000 28-byte entries fit, can each be edited once, and the
   store stays consistent; filling beyond capacity gives "dictionary full" with
   lookups still working.
8. **Wear.** Over the model-check run, no segment's erase count exceeds three
   times the mean.
9. **Host image.** Segments from `segment_image.py` pass the firmware's import
   checks compiled on the host; one flipped bit anywhere is rejected.
10. **Old format.** Version-2 snapshot data opens as an empty dictionary.

On hardware afterwards, with ~190,000 entries loaded: time add, edit and delete
from the browser tool (target: well under 100 ms when no segment is opened);
pull USB power during a burst of adds and during a rebuild; time a forced
rebuild; type steno during cleaning and during a rebuild.

## 12. Measure, then tune

- Table rebuild time near capacity. The first lever is the flash clock
  (cleanup spec P5). If it is still too long, allow adds during a rebuild by
  writing new slots to both the old and the new table.
- Block erase time when an add opens a new segment (typically ~150 ms on this
  chip family, worst case much longer). If it is noticeable, pre-erase the next
  head segment in the background.
- The 87 % table limit, 256-slot probe limit, 8-segment cleaning threshold,
  32-record slice and 500-entry import threshold are starting values.
