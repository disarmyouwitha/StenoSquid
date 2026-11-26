# Engineering notes and roadmap additions: 2026-11-25

Everything here is labeled **measured** (a number from a build or a run), **from code** (read from source, not
yet timed) or **proposal**. Nothing in this file has been run on hardware yet.

## 1. Roadmap items added

### R1. Heap and stack measurement (explicit item)

Before today the plan mentioned "heap/stack peaks" only as a metric inside the Phase 1 gate list; there was no
budget and no pass/fail rule. This is now its own item, and it gates any further RAM growth.

- **Why.** The everyday 32-slot build is at **91.88%** of RAM (240,854 B) and **92.23%** with the transfer option
  (measured, link map). Static size says nothing about peaks, so the real margin is unknown.
- **What exists (this branch).** `CONFIG_ZMK_STENO_SQUID_MEMORY_DIAGNOSTICS` (append
  `config/boardsource-wireless-corne/memory.conf`) fills every stack with a pattern, counts the system heap peak, and
  serves both through the USB-only console command `memory_report`. `tools/bench/run_memory_report.py` prints the
  tightest stacks first, flags any below 25% free, and saves the raw JSON under `build/memory-report/`. The reader is
  unit-tested; the firmware side builds but has not run on a keyboard. The combined measurement build (32-slot everyday +
  transfer + memory diagnostics, Boardsource left) measured 506,156 B flash and **242,078 B RAM (92.35%)**, which is 312 B
  more RAM than the same build without the diagnostics.
- **Measurement matrix (proposal).** Read after each workload from a fresh boot: idle; 300 WPM sustained and 350 WPM
  burst replay on the full 32-slot pool; a Base64 import; a binary transfer; Bluetooth typing plus management;
  ZMK Studio connected; a cold boot validation. Record peak heap, every thread's stack high-water mark and the ISR
  stack.
- **Policy (proposal).** Every stack keeps at least 25% free at its worst measured case. The heap is sized to its
  measured peak plus an explicit margin. Anything the data frees is reported before it is reclaimed.
- **Where the first suspicion points (from the link map).** The system heap is a fixed 32,768 B (13.6% of RAM) and
  nothing has measured how much of it is used. Stacks: engine 8 KiB; main, system workqueue, Studio RPC and HID worker
  4 KiB each; dictionary flash 3 KiB.

### R2. Move the transfer loop out of Python into JavaScript

- **Evidence (measured, Node 22, no-op browser side, real bridge shape).** One Python-to-browser call costs about
  **650 µs per write and 515 µs per read** (JSON encode, JSPI suspend, decode). A windowed upload needs roughly one
  write and one acknowledgement read per 49-byte frame, so the Python-driven client tops out near **40 KiB/s**, about
  today's USB HID rate and far below the 250 KiB/s research target. The bridge, not USB, is the ceiling for the
  Python client.
- **Design (proposal).** Python (or its successor, section 1.3) plans the update and hands over one finished record
  stream. A JS `TransferClient` using `tools/manager/browser/transfer_frame.js` runs the windowed protocol directly on
  WebHID and returns one result. Same wire format, same fallback rules, same tests (the scripted protocol tests port
  one for one).
- **Gate.** Browser-measured useful payload rate, JS path versus Python path, on the same keyboard and fixture.

### R3. Manager direction: one standalone HTML, no Python, no CLI

**Decision recorded:** the Manager is a standalone HTML app run in Chrome. There is no command-line tool and no
backward compatibility with one. The Python-in-browser design exists only because the Manager started as a Python
program. It was audited and measured in `docs/manager-audit-2026-11-25.md`, which supersedes the earlier
"compile the firmware's C++ to WASM" framing:

- **Measured:** a typed-array JS compiler is byte-identical to the Python compiler on the real dictionaries and about
  4-5x faster (0.4-0.6 s versus 1.7-2.5 s native, 2.6-3.0 s in Pyodide); the decoder is 10-13x faster and
  identical entry for entry. Pyodide adds 2.5-3.4 s of startup and an 11.6 MiB runtime, runs on the UI thread, and
  every HID report crosses a JSON bridge.
- **Decision (owner, after review):** the Manager is written in **JavaScript**. TypeScript is optional, never a
  requirement, so no build step is needed for the standalone HTML. The plan is the existing HTML/CSS, workers for
  heavy work and a JS transfer session over a pluggable transport, ported in gated steps M0-M6. Python is removed
  from the product, not from the laboratory (section 3). WebAssembly stays optional: the firmware's own validator
  as an in-browser dry run, only if it shows value. There is no WASM-first milestone.
- **Gate for every port step:** output byte-identical to Python or accepted by the C++ simulator
  (`transfer_device_sim`, the real `NativeStage`).

**M1 status (2026-11-26, first half): the catalog and journal reader is ported.** `tools/manager/browser/snapshot.js`
ports `Snapshot` (strict, metadata-only and normal reads, both pool sizes), the journal record codec and the metadata
planner (move, rename, delete) from `native_update.py`, with the same checks in the same order and the same error text.
The reader takes a sync or async `read(address, length)`, so the same code will read a keyboard or a captured image.
Gate: `golden_snapshot.py` records, from Python, every flash read and the result (or error) for 28 images (clean,
journal tail, corruptions, boundary cases) and 58 metadata plans; `test_snapshot.mjs` replays them and requires the same
parsed state, the same error text and the same read pattern. Both run in `test_js_transfer.py`, and `--check` fails if
the committed vectors are stale. Mutation check: the first vector set let five deliberate mutations of the port survive;
the added cases (duplicate sequence, checkpoint boundary, repeated index entry, NUL and out-of-range strokes, move upper
bound) catch them. Three exact-limit constants (64 rows, 253 extents, 256 tail records) are copied verbatim and not
exercised at their boundary. Second half: `device_reads.js` ports `update_command`, `read_flash`, `read_live_journal` and `read_snapshot`
(including the journal-trimming mode, which reads about 2 KB instead of 66 KB for the same snapshot) over any object with
`command(text, timeout)`. Gate: Python's own functions run against a fake console on 24 images and failure modes
(busy, short read, bad ownership bitmap, malformed committed records); the JS port must send the same commands in the
same order and reach the same state, error and byte count (`test_device_reads.mjs`).

Third part (M1 lookup reader): `decode.js` ports `format_stroke`, `compact_entries`, the JSC4 reader checks,
`require_snapshot_hint` and `effective_entries` (a compiled base with the journal's sets and resets applied), keeping
Python's validation so a damaged image is rejected. Gate: 1,017 cases (intact images with and without a flash base,
two-member images, 860-plus single-byte flips and truncations, targeted damage to the maximum length and bitmap mask,
the SHI1 hint, and catalog rows with a bad member or base); `test_decode.mjs` requires the same entries, the same error
text where the code wrote one, and acceptance of exactly what Python accepts. It supersedes the spike
`tools/bench/js/decode_dict.mjs` (still byte-identical to Python on the real Lapwing and Plover images).

**M2 status: the update planner is ported.** `plan.js` ports `plan()` (personal edits, named-dictionary import,
journal compaction, move/rename/delete, the allocation order, journal and index layout, catalog payload). Gate:
`golden_plan.py` runs Python's planner on 5 images (small and large pools, a multi-journal catalog with real garbage, a
three-journal ordering case, an exhausted identity counter) for 49 cases, recording each staged segment's length and
SHA-256 in upload order plus the catalog payload; `test_plan.mjs` requires byte-identical results or the same error.
Byte-identical output to Python is the spec's alternative to C++ simulator acceptance (Python's plans are what the
simulator already accepts), so a separate simulator run of JS plans adds nothing; it becomes useful only when the JS
planner stops being checked against Python at M6. Mutation check: 25 deliberate mutations; the survivors are
comparisons that no fixture can reach (an exact `live == remaining` tie, a last-journal anchor that equals the
anchor, the 253-extent limit, an unreachable 25-bit stroke test). While adding these vectors I found and fixed a bug
in my own `golden_snapshot.py` helper (`append_tail` stepped by the unpadded record length, so earlier "journal tail"
vectors only exercised part of their records; JS still agreed with Python, now on all of them).

**M2 status, second part: the snapshot, change, merge and import planners are ported.** `snapshot_plan.js` ports, from
`native_snapshot.py` and `native_import_plan.py`: the stage allocator, `plan_named_snapshot`, `plan_change` (consolidate,
convert, clear, delete, merge), `merge_rows`, `merged_entries`, `journal_baseline`, `inspect_free`, `consolidation_cost`,
`plan_compact_import`, and the import, merge and offline-merge token digests (Python's `json.dumps` and SHA-256, byte for
byte). Compiled images enter as plain data, so the compiler is gated separately. Gate: 131 golden cases from Python
(chained imports to get real compact rows; patched catalogs for the identity-counter limit, the 253-extent limit, each
merge-pair condition, a shared extent and out-of-range identities; journal tails with resets and entries of both merged
identities; scattered free segments so the staging order is not ascending), plus replayed flash reads for
`merged_entries`; results are byte-identical or fail with the same error. The vectors caught one real porting mistake
(Python escapes DEL, 0x7f, in `json.dumps`, which would have changed every token whose serial contained it). Mutation
check: 45 deliberate mutations, all caught except unreachable ones.

**M2 status, third part: the factory-package planner is ported.** `package_plan.js` ports `plan_package` (install and
uninstall of the six built-in dictionaries: row and extent edits, renumbering of later bases, the free-space report and the
confirmation hash that the user approves, plus the serial-bound approval digest). Gate: 62 golden cases from Python on
images with some packages absent, patched catalogs for every refusal (shared extent, snapshot flag, base past or equal to
the catalog, a named member, a renamed row, 252 and 253 extents), tight and scattered free space, and both pool sizes;
byte-identical or the same error. Mutation check: 25 mutations, all caught except two that cannot change a result.

Not yet ported: `backup_all`, `overlay_usage`, `plan_replacements` (replacing a pinned base; needs the JSC4 lookup reader),
the compiler's use inside `native_import_plan.named_import` and `merge_snapshots` (the console-driven orchestration, M3), repack
(`native_repack.py`, M4) and recovery (M4). None of the JS pieces is wired into the Manager UI yet (M3/M5).
## 2. Findings

### RAM by owner (measured, 32-slot everyday build with the transfer option, link map)

| Item | Bytes |
| --- | ---: |
| system heap (`CONFIG_HEAP_MEM_POOL_SIZE`) | 32,768 |
| `StenoEngine::container` | 25,004 |
| Native `store` | 11,432 |
| Native `cells[]` (32 slots) | 11,008 |
| `Console::instance` | 8,288 |
| `dictionaryList` | 8,232 |
| engine thread stack | 8,192 |
| main / system workqueue / Studio RPC / HID worker stacks | 4,096 each |

- 32 slots cost about **5.5 KB** over 16 (235,350 to 240,854 B), not the roughly 1 KiB the handoff recalled.
- The default build already includes a USB CDC-ACM interface for ZMK Studio (two 1 KiB ring buffers plus a 1 KiB
  workqueue stack). Phase 3 must plan around that existing interface and its endpoints.

### Boot time on the full 64 MiB pool (from code, not yet timed)

- Base dictionaries are already skipped through a stored fingerprint (`trust_bases`).
- Boot validation still walks every journal record and every forward and reverse index entry with random record reads.
  In unmapped mode each read is its own blocking QSPI DMA transaction with no read-ahead.
- Codex measured 1,201,627 logical reads for a boot (68 s): about **56 µs per read** on average, so boot time is
  roughly proportional to the number of reads.
- **Proposals, in order:** (1) per-phase timers and read counts (catalog, journals, forward index, reverse index,
  extent CRCs) so the dominant phase is known; (2) persist a fingerprint of the index and journal state at publish
  time, because publication already validates them fully, and skip the walk at boot when it matches, finishing a deep
  check in idle time; (3) optimistic open: ready after structural checks, deep check in idle slices, read-only fallback
  if it fails; (4) read-ahead for sequential index passes and larger transfers for CRC passes. Items 2 and 3 are the
  same trust pattern the code already uses for bases.

### Transfer review findings (Codex) and resolution

1. **Busy NAK offset.** With the flash lock busy, the NAK carried the refused frame's offset instead of the
   receiver's confirmed one, so the client could skip data the keyboard never accepted. Confirmed by a failing test.
   Fixed in `transfer_port.h` (NAK carries the receiver's offset, or 0 outside a session). The client also now ignores
   any offset beyond what it has sent.
2. **NAKs kept the stall timer from firing.** Stall detection looked for silence rather than lack of progress, so a
   peer sending endless busy NAKs kept the transfer alive. Confirmed by a hanging test (a peer that NAKs on every
   read). Fixed: a stall is now "the acknowledged offset did not advance", whatever else arrives. END and COMMIT
   waits remain bounded by their deadlines (120 s and 1800 s).

## 3. CLI and server removal

Removed: `stenosquid.py` (a 263-line command-line tool and the launcher for a local Python web server), its CLI-only
tests, and the Manager's local HTTP server (`Jobs` and `serve` in `manager.py`) with its test. Active READMEs and docs
now point at `StenoSquidManager.html`. The serial diagnostics (`--diag`) moved to `tools/bench/diag.py`, a lab tool.

Still present, listed for a decision: (a) the `hidapi` USB console (`import_personal.open_console`) and the scripts in
`tools/bench/` that drive hardware through it, including Codex's replay tooling; (b) dead local-server code in
`manager.js` and `index.html` (a `fetch` backend and a session-token placeholder); (c) historical references in
`docs/archive/`, left untouched as a record. **Decision (owner, after review):** (a) stays. Python development and
hardware-testing scripts, and the independent test oracles, remain in the repository. Removing Python from the product
does not remove it from the laboratory.

## 4. Open decisions

1. ~~Allow binary uploads over Bluetooth?~~ **Decided 2026-11-26: no.** Binary transfer stays USB-only until Bluetooth
   reaches at least 50 KiB/s (measured today: 0.61 KiB/s, section 7.9). Implemented as
   `CONFIG_ZMK_STENO_SQUID_TRANSFER_OVER_BLUETOOTH` (default n): over Bluetooth the `transfer-capabilities` probe is
   refused, so clients fall back to the existing text paths, and binary frames are ignored. Only the lab fragment
   `transfer.conf` turns it on, so the Bluetooth throughput work can continue. Revisit when a Bluetooth run reaches 50 KiB/s.
2. **Decided 2026-11-26:** keep `heap-reclaim.conf` as an opt-in lab fragment; it is not applied to shipping builds. The
   engine peaks near 5 KB of its ~20 KB arena at 32 slots, so there is no need to reclaim unless a feature needs the room.
3. Keep the transfer wire format (BEGIN fields, record format) labeled experimental until approved.

Settled by the owner after review: JavaScript is the Manager language (TypeScript optional); the Python lab scripts
and oracles stay; the memory diagnostics stay available permanently (off by default); final qualification is
cross-tested on the saturated 64 MiB board. The earlier WASM-first milestone (W0) is withdrawn and not on the plan.

## 5. Cleanup after the first hardware tests (do not forget)

Nothing below is urgent and none of it should happen before the Boardsource results are in.

| Item | What to do | When |
| --- | --- | --- |
| Memory diagnostics (`memory_report`, `memory.conf`, `run_memory_report.py`) | **Keep permanently** (owner decision): off by default, and a standing tool for future firmware work. Nothing to remove; after the data is in, only right-size the RAM | After tonight's runs |
| **DONE: 32 slots is the shipping default** | Owner approved on 2026-11-25 after the measurements in 7.5 and 7.6 (engine heap peak about 5 KB of 20 KB with the full pool; all stacks at least 32% free on the Boardsource). The Kconfig default (`ZMK_STENO_SQUID_NATIVE_DICTIONARIES`) is now 32, so CI Default builds are 32-slot. Still unmeasured: Bluetooth to a host, Studio, sustained typing; re-check if any of those shows a stack under 25%. `phase1-everyday.conf` and the Measure builds' explicit 32 are now redundant but harmless | Done |
| Transfer option (`CONFIG_ZMK_STENO_SQUID_TRANSFER_SESSION`) | Stays opt-in until ratified; decide its default and the Bluetooth question | After hardware results |
| JS spikes (`tools/bench/js/`, `manager_workloads.*`, `test_js_compiler.py`) | **Keep.** They are the seed of the new Manager core. Delete only the superseded naive port (`compile_dict.mjs`) and `phases.mjs` once `compile_fast.mjs` moves into the Manager source | At M1 |
| Dead Python (1,367 unreachable lines in the shipped bundle) | Delete | Now or at M6 |
| `native_transfer.py` | Retire when the JS transfer session lands | M3 |
| Pyodide, `manager.py`, `bridge.py`, remaining Python Manager code | Retire | M6 |
| Lab scripts that use `hidapi` (`tools/bench/*.py`) | **Keep** (owner decision), along with the Python test oracles | n/a |
| Dead `fetch`/token branch in `manager.js` and `index.html` | Remove with the de-polling work | M5 |
| `docs/archive/` references to the old CLI | Leave as a historical record, or mark them archived | Anytime |
| Branches and worktrees (`claude/phase2-transfer`, `claude/phase2-integration`, `StenoSquid-phase2`, `StenoSquid-int`) | Delete once merged to `main` | After merge |
| Local UF2 files in `build/uf2/` | Untracked; delete when superseded | Anytime |

## 6. Work split: Codex phase and Claude phase

Folded in from the GPT handoff (revision 5) after review. Items marked "from v5" are kept from that document.
Everything is opt-in, nothing here changes a shipping default or a storage format, and nothing destructive runs on
hardware without explicit approval. Report "passed", "failed" and "not run" separately, and keep RAM-only,
scratch-flash and end-to-end numbers apart (the earlier 15-16 KiB/s BLE figure was RAM-only).

### 6.1 Hardware, one owner at a time

| Board | Flash | Owner | Used for |
| --- | --- | --- | --- |
| Boardsource Left/Right (daily driver) | 64 MiB | Codex phase | Boot time, near-full pool, sustained typing |
| Handwired nice!nano Left (owner wired a W25Q128) | 16 MiB | Claude phase | Memory, transfer, JS client, throughput |

Correction: an earlier note said the nice!nano has no dictionary flash. The owner's handwired build does (W25Q128,
16 MiB; `config/nicenano-wireless-corne/qspi.dtsi`), and the transfer and memory options are not board-restricted.
To do: build a nice!nano Left with `transfer.conf` and `memory.conf` appended and check its SRAM link map first (the
nice!nano config differs, and no nice!nano transfer build has been made yet). The owner's build is the `nicenano.zip` asset
of release v0.1.8 (Left.uf2, Right.uf2, StenoSquidManager.html); that release predates this work, so it is the
baseline, not a transfer-capable build. A 16 MiB board covers everything
except 64 MiB high-address behavior, which stays simulator-tested here and hardware-tested in the Codex phase.

**Final qualification (owner decision):** the 16 MiB board alone cannot qualify the combined release. Once the
transport (wired and Bluetooth) is complete, it is cross-tested on the saturated 64 MiB Boardsource board: upload,
download, backup and repack against the near-full pool, with high addresses exercised, before any release claim.
Results on the nice!nano count as development evidence only.

### 6.2 Codex phase

| Item | Source | Gate |
| --- | --- | --- |
| Boot-time regression: 68 s and 38.8 s on the near-full pool against about 4 s recalled. Profile first, then fix. Targets 3-5 s normal and 10 s at most near-full. Never trade safety or the 512-event queue | v5 1D | Stage timings, five cold boots per configuration, before/after on the full fixture |
| Near-full 64 MiB stress with exact per-stroke oracle (derived from the fixture manifest, not firmware output) | v5 1B | Integrity and translations before/after |
| Dormant-library matrix, cases A to D: 32 installed with 4 enabled must perform like a minimal library (5% p95/p99 relative gate) | v5 1C | Identical output, zero disabled probes |
| Active-stack sweep (4 to 32 active; large bases and edits) and 300 and 350 WPM replay | v5 1C | Zero loss, bounded backlog |
| Harness rule: persist the raw trace, expected and actual text and status before the capture app tears down (it crashes at end of runs) | v5 | Distinguish harness from firmware failure |

### 6.3 Claude phase

| Item | Gate |
| --- | --- |
| R1 memory report on a cold boot, idle, typing replay, import, binary transfer and Bluetooth. Record the post-boot idle marks separately from the boot spike. Pass: 25% minimum stack headroom, an explicit heap margin, no safety-queue reductions | Table plus raw JSON |
| Throughput before anything else changes: raw WebHID to scratch region with a synthetic payload (no planner), then a staged upload, in real Chrome | Useful payload bytes per second, per stage |
| R2 / M3: direct JS transfer session over WebHID, ported NAK-flood and busy-NAK regression tests, measured on the same fixture | Same results as the Python client |
| M1 and M2: JavaScript verify, lookup, catalog parsing, allocation and planners, with golden vectors and C++ NativeStage simulator acceptance. JavaScript is approved as the implementation language; TypeScript is optional and not required | Byte-identical plans |
| Phase 3 research: CDC-ACM with Web Serial, checked against ZMK Studio coexistence; WebUSB only if CDC falls short; no new USB configuration ships without approval | RAM-only, scratch-flash and end-to-end rates |
| Phase 4A and 4B on the BLE path: binary over the 63-byte reports, a bounded asynchronous TX queue (today depth 1 with a busy guard; distinguish notify acceptance from completion; verify against source) | Same three rates, plus input latency |

### 6.4 Shared and approval gates

- Each phase hands back: commit and build config, reproducible instructions, before/after table, raw logs, tests
  passed/failed/not run, memory and latency impact, remaining risks, and a recommendation.
- Needs explicit approval first: any shipping default, storage format, destructive hardware action, reduced
  CRC or queue guarantee, new production USB or BLE interface, and any no-RAM-regression exception.
- Dropped from v5: its revision bookkeeping and duplicated links (stale on the next push).

## 7. First hardware results (nice!nano Left, 16 MiB, 32-slot Measure build `48e5378`, USB)

All **measured**, single runs, one board. Not a qualification.

| Measurement | Result |
| --- | --- |
| Link map, 32 slots + transfer + diagnostics | 242,166 B RAM (92.38%); 507,388 B flash (62.56%). The 16-slot version was 236,662 B (90.28%) |
| `transfer-capabilities` | `OK XFER1 49`: the transfer session is live on real hardware |
| Binary install, Lapwing Commands (4,300 B) | 0.65 s, binary path |
| Binary reinstall, Lapwing Base (1,831,220 B staged, 114,885 entries) | upload **45.23 s = 39.5 KiB/s** including erase; whole operation 49.53 s; identical size and entry count afterwards |
| Upload and verify, Lapwing Proper Nouns, aborted before commit | 7.6 s; catalog unchanged |
| Idle memory, 106 s after boot | every stack at least 32% free (tightest: BT LW WQ 32.3%, BT RX pri 34.8%); heap peak 0 B of 32,768 B |
| Memory during upload and verify | `javelin_web_hid` 2,020 of 4,096 B (50.7% free); `dictionary_flash` 1,812 of 3,072 B (41.0% free); no other stack moved; heap peak still 0 B |
| Command round trip, native Python `hidapi` | 1.02 ms |
| Command round trip, Chrome 157 WebHID | 1.39 ms |
| Flash read of 1 MiB (Base64 console), Python `hidapi` | 43.3 KiB/s |
| Flash read of 1 MiB (Base64 console), Chrome WebHID | 41.1 KiB/s (95% of Python) |
| `sendReport` alone, Chrome, awaited one at a time | 1,607 reports/s |

What this does and does not say:

- The 1 MiB read moves about 905 reports/s of Base64 text, so the USB HID input endpoint (about one report per
  millisecond) is the limit, not the browser. Binary frames carry 49 payload bytes per 63-byte report against
  about 46 for Base64, so on HID alone the gain is roughly 5-10%. The 250 KiB/s target needs CDC (Phase 3).
- The upload direction has headroom: Chrome can send 1,607 reports/s, about 77 KiB/s of 49-byte payloads, yet
  the upload ran at 39.5 KiB/s. Section 7.1 shows why.
- Not measured: the real Manager (Pyodide) path in Chrome, a JS transfer session, Bluetooth, Studio, sustained
  typing, or the heap under any load. The heap showing 0 B must not be acted on until the instrument is confirmed
  to see the allocations that matter.
- **Correction.** An earlier version of this section listed "console unreachable for over 90 s after the uninstall
  commit" as unexplained. That was a bug in the test script, which tried to reconnect about 3 s after the commit
  and treated its first failed attempt as a timeout. Measured properly (7.1), the console is reachable about
  3.5 s after the commit finishes.
- Lapwing Base reinstalls disabled (the restore path does not re-enable it).

### 7.1 Where the upload time goes (Lapwing Base reinstall, 1,831,220 B staged, two runs, binary path)

| Stage | Run 1 | Run 2 |
| --- | --- | --- |
| BEGIN to open | 40 ms | 23 ms |
| Streaming 1,881,392 record bytes (includes record headers) | 43.79 s (42.0 KiB/s) | 43.85 s (41.9 KiB/s) |
| END to VERIFIED | 0.00 s | 0.00 s |
| COMMIT to DONE | 1.28 s | 1.28 s |
| Whole `upload()` | 45.12 s | 45.16 s |
| Useful payload rate end to end | 39.6 KiB/s | 39.6 KiB/s |
| Frames sent, one per 49 bytes | 38,396 (877 per second) | 38,396 |
| Acknowledgements received | 38,396, one per frame | 38,396 |
| NAKs, rewinds, resent frames, stalls | 0 | 0 |

- The protocol itself costs nothing here: no NAK, rewind, resend or stall in either run.
- The keyboard sends one acknowledgement report for every data frame, so the input endpoint carries about 877
  reports per second, the same ceiling the Base64 read test hit (about 905 per second). **The upload looks limited
  by acknowledgement traffic on the USB HID input endpoint, not by flash or by the sending side** (Chrome can send
  1,607 reports/s). This is an inference from matching rates, not yet a controlled experiment.
- Flash erase is visible but small: 29 erases, with the longest single pauses of 218-248 ms after an erase (about
  6-7 s in total, roughly 15% of the stream stage, inferred from those pauses, not timed separately).
- Proposal: cumulative acknowledgements (acknowledge every few frames, and at END), which the window of 8 already
  allows. If acknowledgement traffic is the limit, this should lift the rate toward the sending ceiling, perhaps
  60+ KiB/s over HID. It is a wire-behavior change to an experimental protocol, so it needs owner approval.
- Where the firmware does its read-back verification (during streaming or inside the 1.28 s commit) is not
  established; END to VERIFIED is instant.
- Reboot to console: about 3.5 s after the commit finishes.

### 7.2 Result of cumulative acknowledgements (same board, build `acaca31`, same Lapwing Base reinstall, two runs)

The receiver now acknowledges every window/2 frames (4 with the client's window of 8), on the last frame and on a
short frame (`docs/transfer-session.md`). Same test, same board, same client; only the firmware changed.

| | Per-frame ACK (7.1) | Cumulative ACK | Change |
| --- | --- | --- | --- |
| Streaming stage | 43.79 s / 43.85 s | **25.47 s / 25.65 s** | 42% shorter |
| Whole `upload()` | 45.12 s / 45.16 s | **26.81 s / 26.98 s** | 40% shorter |
| Useful payload rate end to end (1,831,220 B) | 39.6 KiB/s | **66.7 / 66.3 KiB/s** | +68% |
| Acknowledgements for 38,396 frames | 38,396 | **9,599** | exactly one quarter |
| NAK, rewind, resend, stall | 0 | 0 | unchanged |
| Catalog afterwards | 114,885 entries, 1,830,444 B | identical | intact |

- This confirms the 7.1 inference: acknowledgement traffic on the HID input endpoint was the limit. Removing three
  quarters of it raised the useful rate from 39.6 to about 66.5 KiB/s, with no protocol errors. It is now above
  the 50 KiB/s stretch target for Bluetooth and far from the 250 KiB/s wired target (still HID).
- The rate is about 1,500 frames per second on average. The 29 erase pauses (216-235 ms) add about 6.4 s, so
  roughly a quarter of the streaming stage is now erase waiting. Hiding them (erasing the next segment while
  data streams) is the next lever, a firmware change, and is not measured.
- RAM: +8 B (242,174 B, 92.38%). Memory during upload and verify is unchanged: `javelin_web_hid` 2,020 B,
  `dictionary_flash` 1,812 B; heap still 0 B. `HID Over GATT Send Work` read 404 of 768 B in this run against
  188 B before; it is 47% free, but the change is unexplained (this run was read 15 s after boot).
- Upload and verify of Lapwing Proper Nouns (aborted before commit): 4.6 s, against 7.6 s before.
- Not yet done: Chrome WebHID with the new firmware (needs the JS transfer session), Bluetooth, and a lost-ACK test
  on real hardware. The host tests cover a lost ACK; the hardware link had no loss in these runs.
- Compatibility: the Python client needed no change. A client that expects one ACK per frame would still work
  (cumulative ACKs are valid) but would not benefit.

### 7.3 The heap the memory report measures is not the heap that matters (static analysis of build `acaca31`, nice!nano 32-slot Measure ELF)

Method: Zephyr `.config`, `nm` and an `objdump -d` scan for every call to an allocator, plus the pinned Zephyr
source (`lib/libc/common/source/stdlib/malloc.c`). Static call-graph evidence, not a runtime measurement.

- **The 32,768 B system heap is unused.** Nothing calls `k_malloc`, `k_calloc`, `k_heap_alloc` or any other
  system-heap allocator. The one conditional path (`queue_insert` allocating a node) is only reachable from
  `k_queue_alloc_*`, and the ELF contains only the non-allocating `k_queue_append`/`k_queue_prepend`. Its only
  references are heap initialisation and our own `memory_report`. So the "0 B peak" reading is correct, not an
  instrument fault. The 32 KB comes from this repo (`config/nicenano-wireless-corne/corne_left.conf:33`, no comment;
  the Boardsource Left build uses the same file), not from Zephyr's defaults.
- **The engine uses the libc heap, which the report does not read.** 24 distinct functions call `malloc`,
  `calloc` or `realloc` (strings, `BufferWriter`, pattern matching, segment lists, the user dictionary,
  `Console::WriteAsJson`, `PoolAllocateData`). With `CONFIG_COMMON_LIBC_MALLOC_ARENA_SIZE=-1` the libc arena is
  every byte of SRAM above `_end`: for this build, 262,144 - 242,174 = about **19,970 B**. That, not the 32 KB
  pool, is the real dynamic heap. The link map's "unallocated RAM" is this heap, so every static byte added
  (32 slots: +5.5 KB) comes straight out of it.
- **Consequences.** (1) The 32-slot decision must rest on the libc-arena peak, which nobody has measured.
  (2) Setting `CONFIG_HEAP_MEM_POOL_SIZE` to a small value would raise the arena from about 20 KB to about
  52 KB for free, a 2.6x increase. This is a one-line change but unproven on hardware (boot, Bluetooth, Studio, USB)
  and I have not made it; it needs approval and a test.
- **Instrument fix, not yet built.** `memory_report` should read `z_malloc_heap` (static in Zephyr, so it cannot be
  referenced directly). Plan: build a `struct sys_heap` whose `heap` points at the arena base
  (`ROUND_UP(&_end, alignment)`, where `sys_heap_init` puts its header) and call `sys_heap_runtime_stats_get`; validate
  the layout assumption against Zephyr's own `heap.c` on the host first, and sanity-check with a `malloc` pointer at
  boot. Alternative: `--wrap` the four allocators in the diagnostics build to count live and peak bytes.
- Until the libc-arena peak is known, treat "92% of RAM used" as a statement about static data only, and the 25%
  stack bar as the only runtime margin we have actually measured.
- **Built, not flashed (the keyboard cannot be reset until the owner returns):**
  - Memory report v2 (`d11f3f8`): reads the libc heap by rebuilding the `sys_heap` view (header at the first
    8-byte boundary above `_end`; `CONFIG_USERSPACE` is off, so no larger alignment applies) and checks that
    free + allocated account for the whole arena, otherwise it reports `ok:0`. It compiles; RAM unchanged at
    242,174 B (92.38%). The layout assumption is read from Zephyr's `sys_heap_init` and `malloc.c` and has NOT been
    confirmed on hardware.
  - `config/boardsource-wireless-corne/heap-reclaim.conf` (`HEAP_MEM_POOL_SIZE=1024`): the override of
    `corne_left.conf` takes effect. Static RAM falls from 242,174 B to **210,430 B (80.27%)** and the libc arena
    rises from 19,968 B to **51,712 B** (2.59x). Build only; not run on a keyboard.
  - Plan once the board is next in the bootloader: flash the report-v2 build, read `memory_report` at idle and
    under a transfer, check the new `libc` line against the 19,968 B arena, then (with approval) flash the
    heap-reclaim variant and exercise boot, USB, Bluetooth, Studio and typing.

### 7.5 First runtime reading of the libc heap (memory report v2 flashed, `d11f3f8`; nice!nano, 32 slots, USB)

Measured. The report's plausibility check passed, and the arena it reports (19,968 B) equals the figure computed
independently from the ELF (`_end` = `0x2003b1fe`, SRAM ends at `0x20040000`), so the layout assumption in 7.3 is
confirmed on hardware.

| Reading | libc heap peak (of 19,968 B) | System heap | Tightest stack |
| --- | --- | --- | --- |
| Idle, about 90 s after boot (marks cover boot and its validation) | 2,564 B (12.8%) | 0 B | BT LW WQ 32.3% free |
| During upload and verify (Lapwing Proper Nouns, aborted before commit) | 2,564 B, unchanged | 0 B | unchanged |
| After enabling Lapwing Base and 500 real lookups (400 random plus 100 multi-stroke, all found, 2.00 ms each over USB) | 2,696 B (13.5%), now 2,564 B | 0 B | unchanged |

- Under these workloads the engine's heap use is about 2.7 KB, with about 17.3 KB of the arena never touched. A staged
  upload adds nothing to it, and 500 lookups against a 114,885-entry dictionary added 132 B.
- This is good news for 32 slots: the margin that mattered (the engine's dynamic heap) is far larger than feared.
  The 92% static figure overstated the risk.
- Still not covered: Bluetooth typing and management, Studio, sustained typing replay, personal-dictionary edit and
  journal paths, several large dictionaries enabled at once. The 32-slot default decision (section 5) should wait for
  those. Reclaiming the unused system heap (7.3) would raise the arena to about 51.7 KB, but on this evidence the
  current arena already has comfortable room, so it is an optimisation, not a necessity.
- Lapwing Base is enabled again (its state before the uninstall and reinstall tests).

### 7.6 Boardsource Left, full 64 MiB pool, memory report v2 (Measure build `8925afc9`, 32 slots, USB; USB only, no Bluetooth host)

Measured. Pool as the owner left it: 32 dictionaries installed, 16 enabled, journal tail 17. Flashed over the daily-driver
firmware (backed up under `StenoSquid-int/build/uf2/`); the external-flash pool was not touched.

| Reading | libc heap peak (of 20,064 B) | System heap | Tightest stack | Store |
| --- | --- | --- | --- | --- |
| Uptime 38.8 s, after boot validation | 4,792 B (23.9%) | 0 B | BT LW WQ 32.3% free | ready, 0 input overflows |
| Uptime 92 s, after about 53 s of typing on both halves (split link over Bluetooth) | 4,864 B (24.2%), now 4,792 B | 0 B | BT LW WQ 32.3% | 0 overflows, input high-water 0 |
| Uptime 119 s, plus 500 console lookups (all found, 4.99 ms each) and a no-change re-enable | 4,924 B (24.5%), now 4,792 B | 0 B | BT LW WQ 32.3% | unchanged |

- With the pool fully installed the engine's heap peaks near 4.9 KB of 20 KB, double the nice!nano's 2.7 KB (32 dictionaries
  installed, 16 enabled), still about 15 KB never touched. Typing and lookups added only 60-130 B.
- Every stack stayed at least 32.3% free on this board (25% bar). `sysworkq` rose to 1,956 B (52.2% free) with typing;
  `javelin_web_hid` to 1,724 B (57.9%) with console traffic; `BT RX` 188 to 220 B with split-link traffic.
- The typing window was short (about 53 s between readings), so this is evidence, not qualification. Bluetooth to a
  host, Studio, Bluetooth management and sustained typing remain unmeasured, as does the replay (Codex's fixture
  file is not on this machine).
- First boot after the UF2 update: the console answered and the store reported ready about 23 s after the flash
  finished enumerating (full open of the pool, `warm_resumes` 0). Boot time is the Codex phase's measurement.
- **A stack to watch.** On the nice!nano a 512-byte stack went from 63.3% to **25.8% free** (380 of 512 B) after a
  real dictionary enable (a state change) followed by lookups. By elimination it is `split_central_split_run_q_stack`,
  the only 512-byte stack object in the ELF. A no-change re-enable plus 500 lookups on the Boardsource left it at
  63.3%, so the trigger looks like an actual selection change, not lookups. Not yet reproduced on the Boardsource,
  where changing the selection would alter the owner's pool. It is just above the 25% bar; if it is confirmed, raise
  that queue's stack size (a ZMK split option) and re-measure.

### 7.7 nice!nano, paired over Bluetooth and typing (report v2 build, 32 slots; USB also connected so the report can be read)

Measured. The owner paired the keyboard, selected Bluetooth output and typed for roughly two minutes (uptime 909.9 s at the
second reading). Whether every keystroke took the Bluetooth path was not verified independently; the stack changes below
are consistent with it.

| Stack (size) | After pairing, before typing | After typing | Free after |
| --- | --- | --- | --- |
| 512 B unnamed (split-central queue by elimination, from the earlier dictionary-enable test) | 380 B | 380 B | 25.8% |
| BT LW WQ (1,400 B) | 972 B | 972 B | 30.6% |
| BT RX pri (448 B) | 292 B | 292 B | 34.8% |
| Low Priority Work Queue (768 B) | 284 B | **492 B** | 35.9% (was 63.0%) |
| HID Over GATT Send Work (768 B) | 404 B | 404 B | 47.4% |
| BT RX WQ (2,200 B) | 1,116 B | 1,116 B | 49.3% |
| libc heap peak (of 19,968 B) | 2,892 B | 2,892 B | 14.5% |

- Typing over Bluetooth moved exactly one stack, the Low Priority Work Queue, by 208 B. Every stack stays above the 25%
  bar; the libc heap, the system heap (0 B), input overflows (0) and the input high-water mark (0) did not move.
- The tightest stacks are now the 512 B one (25.8%), BT LW WQ (30.6%) and BT RX pri (34.8%). None has been tested under
  Bluetooth management traffic (a Manager session over Bluetooth), Studio, or sustained typing. A short typing window
  is evidence, not qualification.

### 7.8 The JavaScript transfer client in Chrome over WebHID (heap-reclaim build `58482f3f`, nice!nano, USB, Chrome 157)

Measured, one run. `tools/bench/webhid_upload.html` (the real `transfer_client.js`, no Python in the data path) uploaded
the same Lapwing Base install plan as every earlier run (29 segments, 1,831,220 staged bytes). Python prepared the plan
(`prepare_chrome_upload.py`); the browser opened its own staged-update session and ran the upload.

| Path | Whole upload | Useful rate | Frames / acks / NAKs / rewinds / stalls |
| --- | --- | --- | --- |
| Python client, native `hidapi`, per-frame ACK (7.1) | 45.1 s | 39.6 KiB/s | 38,396 / 38,396 / 0 / 0 / 0 |
| Python client, native `hidapi`, cumulative ACK (7.2) | 26.8-27.0 s | 66.3-66.7 KiB/s | 38,396 / 9,599 / 0 / 0 / 0 |
| JS client via the Python relay (7.4) | 28.15 s | about 63.5 KiB/s | 38,396 / 9,599 / 0 / 0 / 0 |
| **JS client in Chrome over WebHID** | **28.59 s** | **62.5 KiB/s** | **38,396 / 9,599 / 0 / 0 / 0** |

- Stages in Chrome: BEGIN 57 ms, stream 27.26 s, END to VERIFIED 2 ms, COMMIT 1.27 s. The catalog afterwards is intact
  (114,885 entries, 1,830,444 B).
- The browser path reaches about 94% of the native Python rate with identical protocol behaviour, and clears the
  50 KiB/s stretch target set for Bluetooth, though this was USB. It needs no per-report bridge: the Python-driven client's
  no-op bridge alone capped it near 40 KiB/s (section 2), so this is the M3 spike working as intended.
- Not measured: the Manager's own Pyodide path in the same Chrome (the comparison the audit predicted), Bluetooth, a
  download direction, a lossy link on hardware. One run is not a distribution.
- Lapwing Base was re-enabled afterwards (the restore path leaves it disabled).

### 7.9 The same upload over Bluetooth (JS client in Chrome 157 over WebHID, nice!nano heap-reclaim build, window 8)

Measured, one run. The page asked the keyboard which transport the chosen device used and reported `BLUETOOTH`. Plan:
Lapwing Proper Nouns, 5 segments, 260,328 staged bytes (a smaller package than the USB runs, because of the speed below).

| | USB (7.8) | Bluetooth |
| --- | --- | --- |
| Package | Lapwing Base, 1,831,220 B | Lapwing Proper Nouns, 260,328 B |
| Whole upload | 28.6 s | **417.3 s** |
| Useful rate | 62.5 KiB/s | **0.61 KiB/s** |
| Frames sent / acks / NAK / rewinds / stalls | 38,396 / 9,599 / 0 / 0 / 0 | 5,459 / 1,365 / 0 / 0 / 0 |
| Frame rate | about 1,400 per second | **13.1 per second** (76 ms per frame) |
| BEGIN / verify / commit | 57 ms / 2 ms / 1.27 s | 178 ms / 60 ms / 419 ms |

- The upload is correct (Lapwing Proper Nouns installed with 15,788 entries, 259,528 B; 0 NAKs, rewinds and stalls) but the
  Bluetooth HID path moves about 13 frames, 0.6 KiB, per second. That is about 100x slower than USB and below the 1.71 KiB/s
  the older Base64 path measured over Bluetooth (different host and conditions, so not a controlled comparison). The
  40-50 KiB/s Bluetooth target is not close.
- What it does not say: where the limit is. 13.1 frames per second with a window of 8 means either a round trip of about 0.6 s
  (window / RTT limited) or a fixed rate of roughly one write per 76 ms (a connection-interval limit). Window 16 separates
  them: the rate doubles if the window is the limit, and stays the same if it is a write-rate limit. The negotiated
  connection interval, PHY and MTU were not read.
- Memory during this session was not captured (the commit restarts the keyboard and clears the marks). The next run samples
  `memory_report` over USB during the Bluetooth upload.
- A second run (`?via=bluetooth&window=16`) with `memory_report` sampled over USB during the upload was prepared but not
  run: the sampler covered about 45 minutes with no upload. To resume: `prepare_chrome_upload.py lapwing-proper OUT.json
  --uninstall-first`, start the sampler, then open the page. The keyboard is back in its original state (Lapwing Proper
  Nouns uninstalled, Lapwing Base installed and enabled).

### 7.10 Lost reports on real hardware, and the plain 32-slot build under toggling (nice!nano, USB, overnight run)

Build: the plain Measure image (`NiceNano-Left-Measure-32-libcreport`, no heap-reclaim). Same Lapwing Proper Nouns install (260,328 B, 5,459 frames) through the JS client relayed over `hidapi`, with `STENO_DROP_WRITES` / `STENO_DROP_READS` (seeded, in `tools/bench/js/hw_upload.mjs`) silently losing outgoing reports or discarding incoming ones:

| Loss | Stream stage | Useful rate | Data frames sent | NAKs | Rewinds | Stalls | Result |
|---|---|---|---|---|---|---|---|
| none | 4.29 s | 60.8 KiB/s | 5,459 | 0 | 0 | 0 | installed |
| 5% of replies discarded (79) | 7.35 s | 35.5 KiB/s | 5,483 | 0 | 0 | 3 | installed |
| 5% of frames lost (356) | 16.6 s | 15.7 KiB/s | 7,341 | 1,526 | 275 | 11 | installed |
| both 5% (340 + 158) | 23.5 s | 11.1 KiB/s | 7,250 | 1,330 | 255 | 18 | installed |

Every run completed and was accepted by the device's CRC check, so lost frames and lost ACKs recover on real hardware (the simulator result is now confirmed on the device). A lost reply costs a timeout; a lost frame costs a rewind, which is why it is far more expensive. Real USB loses almost nothing; this is a resilience result, not a throughput one. Lapwing Proper Nouns was uninstalled again afterwards (catalog back to Lapwing Base only).

Memory under toggling: 40 rounds of disable/enable Lapwing Base, each followed by 60 lookups and a `memory_report`. Worst stack was `BT LW WQ` at 32.3% free (948 of 1,400 B), then `BT RX pri` 34.8%, `javelin_web_hid` 36.2%, `dictionary_flash` 41.0%. The 512 B stack that read 25.8% free earlier was 63.3% free here, so that earlier reading did not come from dictionary toggling over USB; the trigger is still unknown. libc heap peak 2,696 B of 19,968 B. Nothing under 25%.

### 7.4 The JavaScript transfer client on a real keyboard (build `81741dd`, relayed through Python `hidapi`)

`tools/manager/browser/transfer_client.js` is a port of the Python upload loop. On the nice!nano (cumulative-ack
firmware), uninstall then reinstall of Lapwing Base, JS protocol logic, HID relayed by `tools/bench/run_js_upload_hw.py`:

| Stage | JS client over the Python relay |
| --- | --- |
| BEGIN to open | 50 ms |
| Streaming 1,881,392 record bytes | 26.80 s (68.6 KiB/s of record bytes) |
| END to VERIFIED | 0.00 s |
| COMMIT to DONE | 1.30 s |
| Whole upload | 28.15 s (about 63.5 KiB/s useful, against 66.7 for the Python client) |
| Frames / acks / NAKs / rewinds / stalls | 38,396 / 9,599 / 0 / 0 / 0 |

- The keyboard accepted the JS client's frames with no protocol difference from Python's, and the catalog was intact.
- The relay adds latency a browser would not, so this is a lower bound, not the Chrome number. The Chrome
  measurement still needs a person to click the WebHID chooser.
- Tests: 12 scripted cases, each original defect reintroduced is caught; acceptance against the real NativeStage
  simulator (clean, plus 8 lossy-link seeds) matches the legacy flash; the JS record stream is byte-identical to
  Python's. Wired into `run_all` through `tools/tests/test_js_transfer.py`.
