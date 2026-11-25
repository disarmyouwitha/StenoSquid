# StenoSquid Manager: audit, measurements and a no-Python design (2026-11-25)

The question: the Manager is a standalone HTML app, yet its logic runs as Python inside Pyodide. Is Python earning
its place, or is it a leftover from when the Manager was a Python program? This document audits the code, measures
the real workloads, and designs the Manager as if Python did not exist, so the decision rests on numbers.

Labels: **measured** (a number from a run), **from code** (read from source), **estimate** (computed from
measurements, not observed), **proposal**.

## 1. Verdict

**Python does not earn its place in the shipped Manager.** Every measurement points the same way:

1. The heavy work (dictionary compile and decode) is pure-Python loops. A typed-array JavaScript port produces
   **byte-identical output** on the real dictionaries and runs about **4-5x faster at compile and 10-13x faster at
   decode** than native CPython, and 6-25x faster than the same Python inside Pyodide, with no 2.5-3.4 s runtime
   startup and no 11.6 MiB runtime to download.
2. Python runs on the page's main thread (there is no Web Worker anywhere), so every compile freezes the UI for
   seconds.
3. Every HID report crosses a Python-to-JS JSON bridge costing about 0.5-0.65 ms, which is a hard ceiling for any
   transport faster than USB HID.
4. Python still has two legitimate jobs today, neither shipped: it is the **oracle** the C++ tests are checked
   against, and it drives the **hardware lab scripts**. Both are reasons to keep it in the repository until its
   replacement matches it, not reasons to run it in the user's browser.

One honest nuance against the strongest form of the argument: at USB HID speed the bridge is **not** what limits
transfers today. HID moves one 63-byte report per millisecond, and the bridge costs about the same per report, so
Python is roughly at par with the HID ceiling. It becomes **the** ceiling only for a faster transport (CDC or bulk).
And the largest operations (backing up or repacking a 64 MiB pool) are limited by transport speed, not by Python at
all (section 4.4).

## 2. Method and caveats

All numbers come from scripts committed under `tools/bench/` so they can be rerun:

| Script | What it does |
| --- | --- |
| `manager_workloads.py` | Times the real Manager code on the real pinned dictionaries (parse, compile, verify, decode, CRC, Base64) |
| `manager_workloads.mjs` | Runs that same file inside Pyodide 0.29.3 (the runtime the Manager ships) under Node |
| `js/compare_compile.py` | Compiles with the Python compiler and the JS compiler, compares **byte for byte**, times both |
| `js/compare_decode.py` | Decodes with `compact_entries` and the JS decoder, compares every entry, times both |

Caveats, so nothing here is overstated:

- Timings were taken in WSL on a laptop, with **Node 22, not Chrome**. They share V8, but a browser adds its own
  scheduling. Run-to-run noise is real: native Python compiled Lapwing in 1.6-3.2 s across runs. Read ranges, not digits.
- The JS code is a spike, not production: the typed-array compiler had one optimisation pass (profile: 33% per-entry
  parse and dedupe, 22% text sort). It is not at its floor.
- Nothing here touched hardware or a real browser. WebHID latency and Pyodide startup in Chrome are unmeasured.

## 3. Audit of the current Manager (from code)

### 3.1 Architecture as built

```
JS UI (manager.js, 1,140 lines)
  -> backend.js: fakes an HTTP job API, polls it every 550 ms
    -> Pyodide (WebAssembly Python, ~11.6 MiB), on the UI thread
      -> manager.py -> native_update / snapshot / repack / recovery ... (Python)
        -> import_personal.Console (text console framing, Base64)  or  native_transfer.py (binary)
          -> bridge.py BrowserDevice.write/read
            -> rpc(): json.dumps -> JSPI suspend -> JS -> JSON.parse     <- every HID report
              -> hid.js -> WebHID -> keyboard
```

Eight layers between a button press and a USB report. In a native browser design the same path is
UI -> operation -> transport -> WebHID.

### 3.2 Code inventory

- Python **shipped inside the HTML**: 7,120 lines in 34 modules (the packager takes every non-test `*.py` in four
  directories). Only **5,753 lines in 22 modules are reachable** from the browser entry points. **1,367 lines are
  dead weight** that ships anyway: `indexed_journal.py`, `journal.py`, `bench_native_import.py`, `exercise_hybrid.py`,
  `exercise_pinned_hybrid.py`, `exercise_binary_records.py`, `binary_journal.py`, `packed_index.py`,
  `serial_tools.py` and three `prepare_*.py` scripts.
- The reachable Python uses **only the standard library** (`zlib`, `struct`, `json`, `hashlib`, `base64`,
  `dataclasses`). Nothing in it depends on a Python-only ecosystem, so every function is portable.
- JS: `manager.js` 1,140 lines, `backend.js` + `hid.js` 361 lines, plus `index.html` and `manager.css`.

Largest reachable modules: `native_update` 671, `hybrid` 601, `native_repack` 596, `compile_dict` 481,
`native_snapshot` 450, `manager` 446, `native_recovery` 329, `manager_editor` 250, `native_base_update` 248,
`native_transfer` 247 (mine), `native_install` 206, `build_native_image` 204.

### 3.3 Findings

| # | Finding | Evidence |
| --- | --- | --- |
| F1 | Every HID report is serialised to JSON, crosses a JSPI-suspended Python/JS boundary, and is converted between lists of ints and `Uint8Array` | `bridge.py` `rpc()` and `BrowserDevice`; measured 650 us per write, 515 us per read |
| F2 | Pyodide and all Python compute run on the **main thread**; compiling blocks the UI for seconds | No `Worker` anywhere in `tools/manager`; compile 2.6-3.0 s in Pyodide (measured) |
| F3 | The UI still speaks the **HTTP-server era job protocol** even in the browser build: `POST /api/job`, then `setTimeout(550)` polling; progress updates arrive up to 550 ms late; a dead `fetch` branch and session-token plumbing remain | `manager.js` lines ~233-278, 732, 1110; `backend.js` `request()` |
| F4 | The 11.6 MiB Pyodide runtime (wasm 8.4, stdlib 2.3, loader 1.0 MiB) is embedded in the HTML, takes 2.5-3.4 s to start (Node), and requires Chrome 137+ for JSPI | measured sizes and startup |
| F5 | Dictionary compile and decode are pure-Python loops: one function (`parse_stroke`) is **63% of compile time** (290k calls, 10.9M `len` calls) | `cProfile` of the real compiler |
| F6 | 1,367 unreachable Python lines ship in the bundle | import-closure analysis, section 3.2 |
| F7 | The Python layer still speaks the **text console with Base64** (33% overhead, one command per 512 bytes); binary transfer is optional | `native_update.publish`, `import_personal.Console` |
| F8 | `json.dumps` of the whole request just to enforce a 48 MiB limit | `backend.js` `request()` |
| F9 | Python is the **reference** for the C++ tests (the planner builds the plans that `NativeStage` tests consume) | `test_native_stage.py` and friends |
| F10 | Large operations are limited by the transport, not by Python (section 4.4) | arithmetic from measured HID rates |

### 3.4 Feature to module map

| Feature | Python modules | Compute character |
| --- | --- | --- |
| Install factory dictionaries (`setup`) | `native_install`, `build_native_image`, `compile_dict` | **CPU: compiles every base from JSON** (about 6 s in Pyodide), then transfer |
| Import / named import / editor apply | `native_import_plan`, `native_snapshot`, `manager_editor` | **CPU: compile + verify every entry** (7-8 s for 115k entries, Pyodide) |
| Open editor / export dictionary | `native_update.effective_entries`, `hybrid.compact_entries` | **CPU: decode every entry** (3.7 s Pyodide) |
| Compact / merge / optimise / package | `native_update`, `native_snapshot`, `native_base_update` | planning (small) + transfer |
| Move / rename / delete / enable / hints / layout | `manager`, console commands | tiny |
| Backup, repack, recovery | `native_recovery`, `native_repack`, `manager_repack` | **transfer-bound** (whole pool) |
| Flash map | `native_layout` | small, read-bound |

## 4. Measurements

### 4.1 Compile, verify, decode (real pinned dictionaries; milliseconds)

Lapwing base: 114,885 entries, 3.47 MB JSON, 1.83 MB image. Plover main: 147,424 entries, 4.2 MB JSON, 2.34 MB image.

| Workload | CPython 3.10 | Pyodide 0.29 | JavaScript (typed arrays) | JS vs CPython |
| --- | ---: | ---: | ---: | ---: |
| Compile Lapwing | 1,600-3,200 | 2,600 | **400** | 4.2x |
| Compile Plover | 1,900-2,500 | 3,000 | **580** | 4.3x |
| Compile 2-dictionary collection | 2,300 | - | **430** | 5.2x |
| Decode all entries, Lapwing | 1,540-1,620 | 3,740 | **148** | 10.4x |
| Decode all entries, Plover | 1,760-2,090 | 3,870 | **137** | 12.9x |
| Verify every entry, Lapwing | 3,030 | 4,220 | not ported | - |
| JSON parse (3.5 MB) | 110 | 100 | 150-340 (ordered scanner) | - |
| CRC-32 / Base64 of 4 MiB | 6 / 13-31 | 2.5 / 20-30 | negligible | - |

- **Byte-identical gate.** All five compile cases (including a non-zero XIP base address and a two-dictionary
  collection) and every decoded entry match Python exactly. Speed only counts because the bytes match.
- Pyodide is only 1.0-2.3x slower than CPython here: the cost is the Python algorithms, not the WebAssembly runtime.
- Pyodide startup: **2.5-3.4 s** before any work (Node); JS has none.
- Compile time per entry: Python about 15-22 us, JS about 3.5-4 us.

### 4.2 The bridge (Node, no-op JS side, real call shape)

650 us per `write` and 515 us per `read`. A windowed upload needs about one write and one acknowledgement read per
49-byte frame, so the Python-driven client tops out near **40 KiB/s**.

### 4.3 What this means per operation (estimates, not browser measurements)

- Install all factory dictionaries (about 4.7 MB of images: Lapwing 1.83, Plover 2.34, extras about 0.5): about 6 s of
  Python compile in Pyodide (2.6 + 3.0 + extras), then roughly 95-120 s of HID transfer at 40-50 KiB/s. JS removes the 6 s and the UI freeze; the transfer is USB-bound either way.
- Open the editor on a 115k-entry dictionary: about 3.7 s (Pyodide) versus about 0.15 s decode in JS.
- Import 115k entries: compile + verify about 7-8 s (Pyodide: 2.6 + 4.2-5.0); the compile half is 0.4 s in JS and the\n  verify half has not been ported.

### 4.4 Where the time really goes: the transport

At HID speed (63-byte report per millisecond, 40-50 KiB/s measured by the firmware docs) a full 64 MiB pool backup
or repack is **about 22-27 minutes**; at the 250 KiB/s Phase 3 target it is about 4.5 minutes. No amount of
JavaScript changes the HID figure. The Manager design must therefore make the **transport pluggable** and keep the
bridge out of the way of a faster one.

## 5. Where Python could still earn a place

Honestly considered, and each fails:

- *Reuse of existing logic.* True, but it is 5.7k lines of dependency-free code that has now been shown portable;
  the port can be gated on byte-identical output against the Python (as done above).
- *Python's libraries.* None are used.
- *Test oracle.* A real role, kept until the port reaches parity, then retired.
- *Lab scripts.* They talk to hardware over `hidapi`; a replacement instrument can be Node-based (section 7).

## 6. The Manager from scratch: no Python (proposal)

Principle: keep the existing look (HTML/CSS) and replace everything underneath it with plain TypeScript. No
framework, no server, no runtime download. One HTML file.

```
UI (existing HTML/CSS, de-polled)           events + promises, no job polling
  -> Operations (async functions)           progress callback + AbortSignal, one at a time
    -> Core (pure TS on Uint8Array)         codec, catalog, planner, journal, snapshot, repack, recovery
    -> Workers                              compile / decode / verify off the UI thread
    -> Transport interface                  WebHID now, WebSerial/CDC later, legacy text console fallback
         -> Transfer session (JS)           binary frames, window, CRC, resume rules (tools/manager/browser/transfer_frame.js)
```

| Layer | Content | Notes |
| --- | --- | --- |
| UI | `index.html`, `manager.css`, `manager.js` | Keep the interface. Remove the fetch/token branch, replace 550 ms polling with direct progress events, replace the `JSON.stringify` size check with `byteLength` |
| Operations | one async function per user action | Same set as section 3.4. Cancellation through `AbortSignal`; a single mutex ensures one device operation at a time |
| Core | dictionary compiler and decoder, catalog and snapshot parsing, allocator and planner, journal records, import/merge/compact planning, repack, recovery bundle | Pure functions over typed arrays; no I/O; fully unit-testable. Compiler and decoder already exist as spikes |
| Workers | compile, decode, verify, hash | Transfer `ArrayBuffer`s; the UI thread never computes |
| Transport | `send(bytes)`, `receive(timeout)`, capability query | WebHID adapter exists. CDC/WebSerial is an option to be proven, not assumed (section 8) |
| Transfer session | windowed binary protocol | The Python client's logic (`native_transfer.py`) ports one for one; its scripted protocol tests port too |
| Legacy console | text commands with Base64 | Small fallback for firmware without the binary transfer (older releases), not for any CLI |
| Persistence | File System Access API for recovery files | Already JS today |

Design options worth taking because JS makes them cheap:

- **Precompile the factory dictionaries in CI** with the same JS compiler and ship the images (plus their pointer
  relocation tables) instead of JSON. Install then needs no compile at all, only relocation to the chosen address.
- **Run the existing C++ simulator in CI** (`transfer_device_sim`, the real `NativeStage` on simulated NOR) against
  JS-produced plans, exactly as the Python planner is tested today. That keeps the safety net while Python retires.
- **Dry-run in the browser** with the firmware's own validator compiled to WebAssembly (the earlier W1 idea) as an
  optional later layer. It is the one place where reusing the firmware's C++ is the point, so it is the one place
  WebAssembly is justified; everything else is simpler and faster as TypeScript.

### Staging, each step gated

| Step | Content | Gate |
| --- | --- | --- |
| M0 (done as spikes) | JS compiler and decoder | Byte-identical to Python on real dictionaries |
| M1 | Verify/lookup reader, catalog + snapshot parsing in TS | Same plans and states as Python on the existing fixtures |
| M2 | Planners (allocation, import, compact, merge, named snapshots) | Plans accepted by the C++ simulator and equal to Python's on golden vectors |
| M3 | JS transfer session and transport interface on WebHID; workers | Browser-measured payload rate versus the Python path (needs the keyboard) |
| M4 | Repack, recovery, package update | Guarded-recovery tests ported and passing |
| M5 | UI de-polling and cleanup | Existing browser test (`test_browser.mjs`) passes; startup and bundle size measured |
| M6 | Retire Pyodide, the Python Manager code and the dead modules | Nothing in the release needs Python; oracle role replaced by golden vectors |

Effort is large but staged: M0-M2 are mostly mechanical ports with a byte-level safety net; M3 is where the speed
shows up; M4 carries the safety-critical risk and should be ported last and tested hardest.

## 7. Assessment of the proposal from the review you shared

Agree, with evidence now behind it: the bridge is the indirection to remove; TypeScript for compilation is proven
(byte-identical, 4-13x); Web Workers for heavy work; keep the HTML/CSS; Pyodide removed unless it proves a benefit
(it did not).

Qualify:

- *"Removed Base64 from our wire protocol."* Only the optional binary session removed it. The Python console path
  that the Manager uses by default still sends Base64 text; F7.
- *"WebSerial: high-speed USB."* Plausible but **unproven**. The default build already carries a CDC interface for
  ZMK Studio; whether a second stream, or sharing that one, reaches the target is a Phase 3 measurement.
- *The bridge as today's bottleneck.* It is the ceiling for faster transports, not for HID (section 1).
- *"TypeScript initially, WASM if justified."* Agreed, and I would name the one justified case: the firmware's own
  validator as a pre-write dry run.

## 8. Measurements still needed (tonight, with the board)

1. Real browser: WebHID `sendReport` and `inputreport` cost per report in Chrome, with and without the Python bridge.
2. Chrome startup of the current Manager (Pyodide) versus a minimal JS shell.
3. JS compile in Chrome on the UI thread versus in a Worker (UI responsiveness during a compile).
4. A transfer of a known update through the JS session versus the Python session on the same keyboard and fixture.

## 9. Decisions for you

1. Approve M1-M2 as the next Manager work (ports gated on byte-identical or simulator-accepted output).
2. Whether the lab scripts (`tools/bench/*.py` using `hidapi`) should be replaced by a Node instrument at M6, or
   stay as the one remaining Python in the repository.
3. Whether to precompile the factory images in CI (changes what is embedded in the HTML).
4. Delete the 1,367 dead Python lines now (a zero-risk size cut) or with M6.
