# Dictionary Phase 0

These tools measure the current design before any storage rewrite. The first
hardware pass is read-only: it does not install, repair, import, reset, reorder
or toggle dictionaries. Existing personal entries, selections and Practice
progress stay intact. Use matching source and firmware.

## 1. Footprint and reproducible inputs

```sh
python3 tools/bench/dictionary_footprint.py
```

Writes `build/dictionary-phase0/footprint/footprint.json` and `footprint.csv`.
It hash-checks pinned sources, uses the real compact compiler and verifies every
source lookup in the generated individual and combined images. It also measures
current single-dictionary log import allocations. Candidate A/B allocation,
reserve, staging and 50k/100k/190k/200k growth figures are explicitly **estimates**;
allocator churn and fault recovery are not simulated by this tool.

Measurement `.bin` files are compiled at one comparison address to measure
space. They are not firmware installation files. Keep using `stenosquid.py`
for ordinary setup and provisioning.

### Tightened Candidate A gate

```sh
python3 tools/bench/candidate_a_footprint.py
```

Reads the hash-verified source files and compact report from the first command.
Builds and validates three host-only shared-log byte images and writes
`build/dictionary-phase0/candidate-a/report.json`. Index probes, exact record and
index allocations, lower bounds and staging assumptions are reported separately.
The `.bin` files are never uploadable keyboard images. This experiment does not
implement catalog recovery or GC; it evaluates space before investing in them.
See [the resulting decision](../../docs/dictionary-storage-decision.md).

## 2. Read-only hardware baseline

Flash this branch's **Left-Diagnostic** image and a matching **Right-Default**
image. Close browser editor connections and leave the left connected by USB.
Install the Python dependencies using `python3 -m pip install -r requirements.txt`.

```sh
python3 tools/bench/run_dictionary_benchmark.py
```

If multiple keyboards are connected, add `--serial USB_SERIAL_NUMBER`.
Do not type during this first lookup-only test. Every named compiled dictionary
gets deterministic one/two/3+ stroke cases where available and a verified missing
outline. These same sampled outlines also go through the existing personal log
and enabled stack; personal-log misses are labeled observations, not equivalent
hit benchmarks. Existing personal entries are exported and checked as hits too.
No synthetic entries are written. `--reverse` adds one reverse query per named
dictionary; it can take much longer than forward lookup.

The result folder contains raw cycle samples, JSON metadata, a CSV summary and a
local personal backup. Reports remain under ignored `build/`; personal data is
not automatically published. The diagnostics use the Cortex-M4 DWT counter at
`SystemCoreClock`, not the lower-resolution system tick. Per-query timestamps
use wrap-safe unsigned differences. Each forward case has 32 timings; reverse
cases have one. Reporting and counter collection happen outside the timed loop.

`xip_word_loads` counts software word loads through the protected XIP helpers in
`0x12000000..0x12FFFFFF`, **not** measured SPI transactions or cache misses.
`personal_read_calls/bytes` count requests to the personal-store adapter; they
are a separate layer and must not be added to XIP counts. Counts are from one
additional untimed query. The compiled counter branches still have a small
instruction overhead when disabled. No cold-cache or isolated thread-CPU claim
is made; elapsed cycles include interrupt preemption.

## 3. Real typing trace

Enter ordinary Javelin, release all keys and focus an empty editor. Then run:

```sh
python3 tools/bench/run_dictionary_benchmark.py --capture-seconds 20
```

Type a known passage, including several multi-stroke words, during the announced
window. Do not change modes or use the browser editor during it. The logger
keeps up to 128 complete chords in RAM and reports overflow separately. It
records final-release handler duration, outline mask, uptime and outcome:
`0` interrupted/dropped, `1` processed, `2` ignored (such as Practice input).
The duration covers lock acquisition, engine processing, hints and display
publication, plus measurement overhead; it excludes queue wait and eventual
USB/BLE delivery. Starting/stopping midway through a chord can omit that chord.
Compare the editor's text yourself: processed input is not proof of the intended
translation, and a successful trace is not yet an automated 200/400 WPM replay.
The diagnostic's old per-stroke serial timing print is disabled in this mode.

## Still required before selecting storage

- An equivalent populated-log corpus and explicit multi-dictionary A/B/C prototypes.
- 10,000-edit/replacement/delete churn and staging/fragmentation measurements.
- Controlled 200 WPM / 300–400 WPM burst replay with exact output checks.
- Contention with GC/rebuilds, idle/deep-sleep wake and near-full tests.
- Runtime peaks beyond the reported HID stack watermark; isolated CPU/current
  measurements where hardware supports them.

Do not turn this initial baseline into a capacity or zero-stroke-loss promise.


## Candidate B reference prototype

The next host-only correctness stage lives under `tools/storage/`. See
[its design, commands and limits](../../docs/hybrid-storage-prototype.md).
It is independent of the diagnostic firmware and never accesses real USB/flash.
