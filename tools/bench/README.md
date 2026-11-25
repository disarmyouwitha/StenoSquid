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
`personal_read_calls/bytes` count reads through the shared Native journal adapter; they
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
[its design, commands and limits](../../docs/archive/hybrid-storage-prototype.md).
It is independent of the diagnostic firmware and never accesses real USB/flash.

## 64 MiB dictionary qualification

The Phase 1 fixture uses pinned factory packages plus seeded synthetic compact
dictionaries and edits. It records corpus hashes, catalog order, allocated
extents and the expected result and owner for each lookup. It contains 49
one-stroke full-stack misses. Their shapes are separate from the synthetic
compact keys, and fixture generation excludes every pinned and seeded key.
Each generated compact includes a one-stroke table.

Prepare pinned sources with `tools/dictionaries/prepare.py`, then build the
offline fixture.

```sh
python3 tools/bench/phase1_fixture.py --out build/phase1-fixture
```

The default corpus enforces space for the firmware's maintenance reserve and
replacement indexes. It retains one small named compact so consolidation can
be preflighted at high addresses. The report gives actual installed and active
counts and the largest free run. `--skip-fill` and the entry-count arguments
allow smoke fixtures. The emitted recovery JSON replaces the target pool when
restored. Use it only on an authorized target with a verified prior backup.

Build the host replay binary using the pinned Javelin source.

```sh
PYTHONPATH=tools/bench python3 - /path/to/javelin-steno <<'PY'
from pathlib import Path
from run_typing_replay import build_engine, compiler
import sys

build_engine(Path(sys.argv[1]), Path("build/phase1-host"), Path("tools/bench/typing_replay.cc"),
             compiler(), optimization="-O2")
PY
python3 tools/bench/phase1_host.py build/phase1-host/typing_replay \
  --fixture build/phase1-fixture --out build/phase1-paced --paced
python3 tools/bench/phase1_host.py build/phase1-host/typing_replay \
  --fixture build/phase1-fixture --out build/phase1-matrix --dormant-trials 3 --active-sweep
```

Host reports check each stroke and lookup owner and record processing latency,
journal reads and bytes, and compact provider calls. Host timing captures the
simulated NOR and the pinned engine. It does not cover QSPI, device scheduling,
BLE, or physical scan timing.

Add `config/boardsource-wireless-corne/phase1-everyday.conf` to the everyday
left build's extra configuration files to open the 32-slot qualification pool
with normal firmware. Use `config/boardsource-wireless-corne/phase1-replay.conf`
for the instrumented device checks and keyboard replay below.

```sh
python3 tools/bench/phase1_device.py --serial SERIAL \
  --fixture build/phase1-fixture/fixture.json --report build/phase1-device.json
python3 tools/bench/run_device_replay.py --serial SERIAL \
  --fixture build/phase1-fixture/fixture.json --wpm 300 --repeat 5 \
  --report build/phase1-device-300.json
```

Use the same replay runner for 350 or 400 WPM bursts with one repeat. Focus the
capture window and leave the keys alone during injection. The replay validates
the injected chord sequence on the board and the final text on the host.
`phase1_device.py` reports the first sample from each distinct miss and the
subsequent same-outline samples separately.

After collecting live-tail replay results, use `phase1_device.py --checkpoint`
with a new report path to publish a replacement checkpoint. This changes
the fixture's catalog and journal state. The check records the physical
replacement index blocks, rechecks integrity, and repeats the lookup oracle.

### 64 MiB hardware results

- The Boardsource pool used 1,006 of 1,023 blocks, with 17 contiguous
  blocks free. It held 32 dictionaries and 80,032 checkpoint records.
- On the instrumented boot, validation traversed both catalog copies
  and finished by 146 seconds of uptime. The earlier seven-minute
  loading stall remains unexplained.
- The everyday 32-slot build from `d8136ea` at 32 MHz reopened the same
  pool 68 seconds after its USB console became reachable following a UF2
  update. The catalog matched the fixture byte for byte, all 233 lookups
  passed, and `KAT` returned `cat`.
- Using injected chords and USB capture, the 44-stroke prose burst passed
  at 349.90 WPM. The completed 300 WPM rerun processed all 1,400 strokes in
  order with zero input overflows. It missed one completion deadline and
  the host received one extra character.
