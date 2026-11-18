# Storage decision: retire Candidate A; continue Candidate B

Status: **Candidate A rejected at the Phase-0 footprint gate. Candidate B is the
selected development direction, pending its design and hardware validation.**

This follows the owner's instruction to tighten A's footprint and stop developing
it if it fails the agreed space target. It does not approve a production storage
rewrite, select an on-flash catalog format, or authorize erasing the keyboard.

## Evidence and reproduction

```sh
python3 tools/bench/dictionary_footprint.py
python3 tools/bench/candidate_a_footprint.py
```

Both use the same six pinned dictionary sources, preserving dictionary identity.
The second command builds host-only byte images at three index occupancy targets.
It verifies all **291,403 forward entries** and **128,552 unique dictionary/text
reverse keys**, including overlapping outlines and repeated translations, then
checks the serialized index contents. These images are experiments, never
firmware upload files. The model source hash, source revisions, input hashes,
image hashes and detailed component accounting are in
[the decision evidence](benchmarks/candidate-a-footprint.json).

Each record extends today's eight-byte record header with a four-byte dictionary
ID, four bytes per stroke, and four-byte-aligned UTF-8 text. Shared forward and
reverse indexes use four-byte image-relative offsets, dictionary-qualified
hashes, linear probing and segment headers. Two catalog generations and three
GC-reserve segments are budgeted. The catalogs/transactions/GC are **not**
implemented by the model; the offsets are not a selected production slot format.

| Index target occupancy | Fresh bytes incl. catalog/reserve | Free bytes | Free after largest dictionary's new records |
| --- | ---: | ---: | ---: |
| 50% | 14,417,920 | 2,293,760 | −2,359,296 |
| 70% | 13,107,200 | 3,604,480 | −1,048,576 |
| 87.5% | 12,451,840 | 4,259,840 | −393,216 |

The largest replacement's records require **4,653,056 bytes**. This staging
scenario assumes a full old/new dictionary version; incremental updates may
need less. It excludes new personal data, fragmentation and replacement index
storage, so it cannot establish a safe update algorithm even where it fits.
The high-load reverse index has materially longer probe chains; those are host
insertion counts, not hardware latency measurements.

## Lower bounds settle the gate independently of allocator assumptions

Today's compiled grouping is **4,639,876 bytes**. The spec rejects A if it costs
substantially more than **2 MiB extra** (2,097,152 bytes).

1. Give A **free IDs, free indexes, free catalog, no reserve, no segment slack and
   no staging**. Current-style records alone still require **8,184,856 bytes**:
   **3,544,980 bytes (3.38 MiB) extra**. This exceeds the rejection threshold by
   **1,447,828 bytes**.
2. Favor A further with hypothetical **three-byte strokes and no text padding**,
   still giving it all those other components for free. With the existing
   eight-byte record header and independent text per record it needs
   **7,031,598 bytes**: **2,391,722 bytes (2.28 MiB) extra**, already beyond the
   threshold. Such unaligned records are not an implemented nRF-safe format.

The full model's densest case costs **7,811,964 bytes extra** before update
staging. Changing catalog size, packing IDs into spare bits or increasing hash
occupancy cannot rescue the current-log-derived A under this gate.

This is **not** a proof that every possible editable representation is too big.
A new compressed, deduplicated editable format could change the result, but that
is a separate research candidate requiring fresh measurements. We are not
spending further device work on the proposed current-log-derived A.

## What continues for B

Keep compact dictionary storage and an editable per-dictionary overlay under a
shared catalog and allocator. Edits remain visible as edits to the targeted
logical dictionary; tombstones hide that dictionary's compact entry and allow
lower-priority dictionaries to win. This preserves the spec's pooled-capacity
goal. Fixed-slot Candidate C is not silently substituted.

Before production implementation:

1. Freeze B's catalog, extent/relocation, overlay identity, reverse lookup and
   reader-lifetime rules. Quantify independent versus grouped image updates.
2. Prototype the allocator and overlays in fake NOR. Measure 10,000 edits,
   deletes/replacements, fragmentation, largest-image staging and near-full
   refusal; fault-inject catalog publication and relocation.
3. Compare compact-plus-overlay lookups and full translation on hardware. Separate
   the measured handler's lookup, hints, host output and waiting time; test
   maintenance contention and controlled exact-output stroke playback.
4. Check power loss, sleep/wake and current on actual hardware. Keep 32/64 MiB
   addressing and hardware support as a separate gate.

No new migration reader or compatibility build. Keep a verified host backup and
require explicit clean provisioning if the future layout changes. No firmware,
flash layout or dictionary data changed as part of this decision.
