# Candidate B: binary records and bounded forward lookup

Status: **development experiment, not integrated or installed firmware**.
This follows the [shared journal](hybrid-overlay-journal.md). Production storage,
the installer and both installed halves are unchanged. There are no migrations.
The subsequent [publication/reverse experiment](hybrid-index-publication.md)
connects the ordered indexes to the fake-NOR journal and adds native reverse cursors.

## Packed records

`tools/storage/binary_journal.py` replaces JSON payloads with an eight-byte prefix:
dictionary ID (uint32), operation (uint8), stroke count (uint8), UTF-8 byte count
(uint16). Up to sixteen uint32 strokes follow, then at most 2,047 text bytes.
Four-byte strokes preserve the engine's extended `^` and `+` bits as well as the
ordinary English keys. Empty translations, tombstones and reset operations remain
distinct. The outer journal retains its sequence, CRCs and commit-last word.

Records are aligned to four bytes, rather than reserving a full 256-byte program
page each. A one-stroke `cat` edit occupies 48 bytes including its 32-byte header.
Programming still splits at physical page boundaries; the final commit word is
four-byte aligned and cannot straddle a page. A torn tail is sealed, never reused.
The fake NOR permits separate programs clearing different bits in one page; real
device program restrictions and XIP/write coordination still need validation.

The Python backend deliberately continues replaying records. It is the recovery
oracle, **not the fast lookup implementation**. JSON catalog metadata also remains
a host reference format.

## Native lookup building blocks

`tools/storage/packed_index.h` reads a sorted immutable table containing an
eight-byte fingerprint and a four-byte record address per entry. Binary search
finds a fingerprint group, then complete dictionary ID and stroke comparisons
resolve hash collisions. Hash equality alone never identifies an outline.
The offline builder refuses groups larger than sixteen; the reader also refuses
an overlong group rather than scanning without a bound. Table CRC, sorted order,
address ownership and collision-group validation must precede catalog publication;
the lookup helper does not replace those checks.

The forward key must encode the stable dictionary ID and the full outline, with
explicit lengths/byte order. The experiment's 200,000-key fixture deliberately
puts four different full keys in each fingerprint group to exercise collisions.
It is synthetic, not a prediction of natural collision frequency.

A fixed recent-edit tail holds up to 256 record addresses in **1,028 bytes**.
Lookup checks that tail newest first, then the immutable checkpoint. Set, remove
and reset remain operations for the caller to apply; the reader returns record
metadata without copying translation text. Full tails must trigger checkpointing
or safe refusal **before** an edit is committed. The native helper has no writer
or automatic checkpoint scheduler yet.

At 200,000 entries, the measured checkpoint-only maximum was **22 twelve-byte
table reads** in the fixture, plus complete-key record checks. A tail miss adds
up to 256 record checks. The general accepted-table bound is 21 binary-search
reads plus at most 17 group reads (sixteen candidates and a terminating check).
Each candidate reads an eight-byte prefix and up to sixteen four-byte strokes;
text is read only after a match. These are operation bounds, **not measured
keyboard latency**. Native tests check safe capacity refusal, old-tail snapshots,
corrupt metadata, failing reads and oversized collision groups.

## Smaller ordered-address alternative

The native experiment also tests sorting by the **complete dictionary ID and
outline**, storing only a four-byte record address per entry. Binary search reads
and compares the referenced record key directly. It needs neither fingerprints
nor a collision-group limit, and compares at most sixteen stroke words per step.
The offline builder must still sort and deduplicate keys and verify the staged
index before publication. The test fixture is already in full-key order.

All 200,000 fixture keys resolved in at most **18 search steps**, using an
**800,000-byte** table. Each step reads a four-byte table entry plus the record
prefix and compared stroke words. Tests cover missing keys, malformed/I/O
failures, dictionary-ID ordering and multi-stroke prefix ordering. This saves
1.6 MB per checkpoint relative to the hash table, while doing more record reads.
It is the preferred space candidate for the next device timing comparison;
actual latency and checkpoint construction have not been measured on hardware.
The ordered search uses the same bounded recent-edit tail, checking newest edits
before the checkpoint. End-to-end publication/recovery is still pending.

## Resource and space limits

Before adding reverse-cursor helpers, the freestanding Cortex-M4 probe,
compiled with Zephyr SDK 0.16.9 at `-Os`, had
788 bytes of text (both search alternatives), no static data/BSS and no undefined allocator symbols. GCC
reports 80 bytes for `ProbeFind`, 32 for its nested table loader, 24 for
`ProbeRecord`, 64 for its record comparator and 40 for `ProbeOrdered`. Callbacks, integration, task stacks,
catalogs and snapshot ownership add costs; these figures are not a replacement
for linking and measuring a full Left-Diagnostic image. The 1,028-byte tail is
caller-owned and therefore is not counted in that probe's BSS. Each copied tail
snapshot would cost another 1,028 bytes.

The hash table costs **12 bytes per indexed outline**, or 2,400,000 bytes at 200,000
entries; the ordered-address alternative costs four bytes, or 800,000 bytes.
Both need recovery copies, records, catalog and staging reserve as well. This is
an overlay index; compiled bases keep their existing indexes. It does not claim
200,000 editable entries will fit alongside all bases. Small-dictionary use has
much lower space cost. Checkpoint write amplification, bounded index construction
and large-overlay capacity remain design decisions to resolve before adopting it.

This capped forward hash table cannot simply be reused for translation groups:
many outlines can share a translation legitimately. The subsequent
[reverse-cursor experiment](hybrid-index-publication.md) provides paginated
ordered-address traversal and host winner filtering without a sixteen-result cap.
It is not yet integrated into keyboard firmware.

## Reproduce

```sh
python3 tools/tests/test_binary_journal.py
python3 tools/tests/test_packed_index.py
python3 tools/storage/exercise_binary_records.py --sources PATH_TO_PINNED_JSON \
  --output build/binary-records.json
```

The codec stress run verifies each of the six pinned sources by SHA-256 and
round-trips every entry. Its [report](prototypes/hybrid-binary-records.json)
compares record bytes, not a proposal to convert read-only bases to journal
records. Native tests are automatically discovered by the existing CI runner.

Before a useful reflash: integrate index publication/recovery and a bounded
checkpoint builder, settle reverse lookup, and add a read-only diagnostic probe
that measures device lookup timing without replacing personal storage. A storage
format switch still requires a fresh personal backup and deliberate provisioning.
