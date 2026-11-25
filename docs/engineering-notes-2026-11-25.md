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

### R3. Manager direction: one standalone HTML, shared core with the firmware

**Decision recorded:** the Manager is a standalone HTML app run in Chrome. There is no command-line tool and no
backward compatibility with one. The Python-in-browser design exists only because the Manager started as a Python
program; it is not a requirement.

**Goal (proposal): compile the firmware's own C++ storage code to WebAssembly** so the Manager and the firmware
cannot diverge, then replace the Python planner and Pyodide entirely.

| Milestone | Content | Gate |
| --- | --- | --- |
| W0 spike | Build `native_catalog/store/stage/validator` (about 3.9k lines, already host-compilable) to WASM behind a small C API; run an existing fixture test inside Node/WASM | Byte-identical results to the native test |
| W1 device model | Read the keyboard's flash map into a WASM-hosted NOR model and dry-run the firmware's own validator and staged commit before any write | A plan the firmware would refuse is refused in the browser |
| W2 planners | Port planning and compilation (import, snapshot, repack, recovery, dictionary compile) from Python to the shared C++ core; Python versions stay as oracles until parity, then go | Golden-vector parity on real dictionaries |
| W3 new UI | Plain JS or TypeScript, no framework, one HTML file built with a bundler, WebHID plus the JS transfer loop (R2) | Startup, bundle size and planning time measured against the current app |
| W4 retire | Delete Pyodide and the Python Manager code | Nothing in the release needs Python |

Risks: effort; toolchain pinning; C++ exceptions and allocation inside WASM; planner semantics re-implemented
wrongly (mitigated by the oracle-parity gate). Today's costs for comparison (measured): the embedded Pyodide runtime
is about **11.6 MiB** (wasm 8.4, stdlib 2.3, loader 1.0), starts in 3.4 s in Node, and needs Chrome 137+ for JSPI.
Code: about 8.4k lines of Python (storage 5.3k, manager 2.1k, compiler 0.6k, dictionaries 0.4k) against 1.5k of JS.

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
`docs/archive/`, left untouched as a record. Option for (a): replace the lab harness with a Node or in-browser
instrument once R3 exists, rather than keep Python only for it.

## 4. Open decisions

1. Allow binary uploads over Bluetooth before Phase 4 proves them? (Today they inherit the console's rules: ordinary
   staged updates only; setup, repack and recovery stay USB-only.)
2. Keep the transfer wire format (BEGIN fields, record format) labeled experimental until approved.
3. What replaces the Python lab harness (section 3a)?
4. Approve W0 (the WASM spike) as the first Manager milestone.
