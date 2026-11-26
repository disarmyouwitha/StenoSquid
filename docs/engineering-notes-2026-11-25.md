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

1. Allow binary uploads over Bluetooth before Phase 4 proves them? (Today they inherit the console's rules: ordinary
   staged updates only; setup, repack and recovery stay USB-only.)
2. Keep the transfer wire format (BEGIN fields, record format) labeled experimental until approved.

Settled by the owner after review: JavaScript is the Manager language (TypeScript optional); the Python lab scripts
and oracles stay; the memory diagnostics stay available permanently (off by default); final qualification is
cross-tested on the saturated 64 MiB board. The earlier WASM-first milestone (W0) is withdrawn and not on the plan.

## 5. Cleanup after the first hardware tests (do not forget)

Nothing below is urgent and none of it should happen before the Boardsource results are in.

| Item | What to do | When |
| --- | --- | --- |
| Memory diagnostics (`memory_report`, `memory.conf`, `run_memory_report.py`) | **Keep permanently** (owner decision): off by default, and a standing tool for future firmware work. Nothing to remove; after the data is in, only right-size the RAM | After tonight's runs |
| **REMINDER FOR THE OWNER: make 32 slots the shipping default** | Today the Kconfig default (`ZMK_STENO_SQUID_NATIVE_DICTIONARIES`) is 16, and CI Default builds are 16-slot; 32 is only in `phase1-everyday.conf`, which the `*-Left-Measure` builds append. When the measurements show 32 slots fit with real headroom (25% stacks, a measured heap margin, and room for the Web Serial/CDC work and everything else planned), ask the owner to approve, then change the Kconfig default and the Default builds. Claude must raise this when the memory results come in | After the memory report data |
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
- The upload direction may have headroom: Chrome can send 1,607 reports/s, about 77 KiB/s of 49-byte payloads, yet
  the upload ran at 39.5 KiB/s. What bounds it (acknowledgement window, flash erase and program time, verify)
  is not yet measured. The next step is per-stage timing.
- Not measured: the real Manager (Pyodide) path in Chrome, a JS transfer session, Bluetooth, Studio, sustained
  typing, or the heap under any load. The heap showing 0 B must not be acted on until the instrument is confirmed
  to see the allocations that matter.
- Unexplained: after the uninstall commit, the console was unreachable for over 90 s from a script, yet answered
  in seconds when checked afterwards. Slow reboot or host HID re-enumeration is not distinguished.
- Lapwing Base reinstalls disabled (the restore path does not re-enable it).
