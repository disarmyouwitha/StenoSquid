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
space. They are not firmware installation files. Use `StenoSquidManager.html`
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

### Full-pool boot timing

Start the timer with the everyday firmware and the 32-dictionary pool in place,
then reboot or power-cycle the left half. The report measures from USB console
enumeration to Javelin reporting ready and verifies the `KAT` lookup. Record cold
boot separately from idle wake within the same boot.

```sh
python3 tools/bench/phase1_boot.py --serial SERIAL \
  --report build/phase1-boot.json
```

For stage profiling, append `config/boardsource-wireless-corne/phase1-boot-profile.conf`
to the everyday 32-slot left build and pass `--profile`. The existing right
firmware can show the layer and dictionary state from the left. Keep it paired
if observing the display. Use a new report path for each of five physical cold
starts per configuration. The trace records each stage's start in device uptime
and cumulative mapped and DMA logical read calls during catalog validation.
Subtract adjacent rows for per-stage elapsed time and read counts. The USB timer
also verifies a Javelin lookup. Keep both measurements so USB enumeration and
dictionary availability can be compared separately. A failed bring-up records
`flash_unavailable` and a subsequent attempt starts again at `qspi_start`.
Pass `--stop-on-unavailable` with `--profile` to save a failed boot without
waking the flash again. On a rejected index extent, the profiler records the
expected and original CRCs. If the original check used mapped QSPI, it then
records uncached DMA and mapped-repeat CRCs in that order on the same flash
power cycle. These reads leave catalog validation's decision in place.

In the host replay of the full pool, reusing extent validation for byte-identical
catalog payloads reduced logical flash reads from 2,162,470 to 1,201,627.
Boot timing after a catalog update leaves two distinct payloads remains to be
measured on hardware.

The simulated full-pool open with matching catalogs issued 320,128 reads in
the forward index pass and 480,094 in the reverse pass. The journal pass
issued 160,300. Another 240,665 reads followed root validation, including
catalog selection and live journal recovery. Use the hardware stage trace to
check which passes dominate elapsed time. Look for redundant checks and reads
first, then measure batching, read-ahead and QSPI changes against the five-second
target. Consider extending persistent trust only if those changes fall short.
Optimistic startup requires explicit approval.

The profiled 64 MiB left build at 32 MHz reached ready 38.1 seconds after USB
appeared following a UF2 update. The journal scan took 9.1 seconds, the index
passes took 7.6 and 9.8 seconds, and catalog selection and live journal recovery
took 11.1 seconds. All 1,204,247 counted reads used mapped QSPI. See the
[post-UF2 trace](../../docs/benchmarks/phase1-startup-profile-after-uf2.json).

The physical off/on attempt hit a USB disconnect and recorded events from two
boots, so it is excluded from timing comparisons. A fresh read of the surviving
boot trace showed both roots rejected before journal scanning and the flash
reported unavailable at 1.346 seconds of uptime. See the
[attempt](../../docs/benchmarks/phase1-startup-profile-physical-attempt.json) and
[cold-boot trace](../../docs/benchmarks/phase1-startup-profile-cold-unavailable.json).
An explicit USB status check later woke the idle flash. The second full open
took 68.3 seconds from QSPI start to ready and recovered generation 2 with all
32 dictionaries. See the [recovery trace](../../docs/benchmarks/phase1-startup-profile-recovery.json).

Reusing the decoded journal key during live recovery and reading 16 index
addresses at a time reduced logical reads in the full-pool host simulation
from 1,201,627 to 971,516. In one post-UF2 run on the device, counted mapped
reads fell from 1,204,247 to 974,136, while the wait after USB appeared fell
from 38.1 to 36.9 seconds. The forward and reverse passes were 1.6 seconds
faster together. Catalog selection and live journal recovery still took
11.2 seconds despite fewer calls. This is above the five-second target.
See the [candidate post-UF2 trace](../../docs/benchmarks/phase1-startup-profile-candidate-after-uf2.json).

On a physical off/on test of the candidate, root 0 rejected the reverse index
at `0x03e90000`, and root 1 rejected the forward index at `0x03e40000`.
Both rejections occurred during extent CRC validation with mapped QSPI reads.
Flash reported unavailable at 1.631 seconds of uptime. The passive observer
did not report ready within 150 seconds. See the
[candidate cold trace](../../docs/benchmarks/phase1-startup-profile-candidate-cold.json).
An explicit USB status request triggered another full open. Javelin was ready
68.0 seconds later. Readback verified all 640,192 bytes of the two rejected
indexes against the original fixture, with both CRCs matching. The catalog
was also byte-identical, all 233 fixture lookups passed, and both roots were
valid. The board was left with the candidate and all 32 dictionaries loaded.
See the [recovery trace](../../docs/benchmarks/phase1-startup-profile-candidate-recovery.json)
and the [readback report](../../docs/benchmarks/phase1-startup-profile-candidate-readback.json).

The same-boot diagnostic at 32 MHz reproduced the failure. One root rejected
a journal at `0x039f0000`. The other rejected the forward index at
`0x03e40000`, which had expected CRC `0xd9cacb6f`. In order, the original
mapped read returned `0x64c9da5f`, uncached DMA returned `0xb5ffc392`, and
the uncached mapped repeat returned `0xbf07df31`. The three completed reads
disagreed with each other and with the expected CRC before the firmware
reported flash unavailable. This rules out a failure limited to mapped reads
in this run. See the [cold diagnostic trace](../../docs/benchmarks/phase1-startup-profile-dma-pair-cold.json).
After the flash idled, a USB status request triggered another full open.
Javelin was ready in 68.6 seconds. Both roots validated, all 32 dictionaries
were loaded with 17 free blocks, and all 233 fixture lookups passed. See the
[recovery trace](../../docs/benchmarks/phase1-startup-profile-dma-pair-recovery.json).

The 16 MHz control adds only `CONFIG_ZMK_JAVELIN_STENO_QSPI_SCK_DIVIDER=2`
to the otherwise identical 32-slot diagnostic build. Following UF2, it
reached ready in 37.0 seconds versus 36.6 seconds at 32 MHz. All 233 fixture
lookups passed, both roots validated, and 17 blocks were free. The recorder
observed no physical off/on during this run, so it gives no evidence about
whether the lower clock avoids the cold-start failure. See the
[post-UF2 trace](../../docs/benchmarks/phase1-startup-profile-16mhz-after-uf2.json)
and [lookup report](../../docs/benchmarks/phase1-startup-profile-16mhz-lookups.json).

On the next user-reported reboot, the 16 MHz control passed the first full
open. Flash reported ready at 91.295 seconds of device uptime, or 90.537
seconds after QSPI startup. The trace recorded 974,136 mapped reads and no
rejected extents or second open. After one layout command timeout, the retry
confirmed both roots valid, all 32 dictionaries, 17 free blocks, and all 233
fixture lookups passing. The host started collecting after USB was already
connected, so there is no USB-to-ready measurement for this run. This single
pass differs from the repeated 32 MHz cold-start rejection. More paired
reboots are needed to determine whether the clock accounts for the difference.
See the [reboot trace](../../docs/benchmarks/phase1-startup-profile-16mhz-reboot.json).

A second physical cold boot at 16 MHz also passed on the first full open.
Ready arrived at 37.436 seconds of device uptime with exactly the same
974,136 mapped reads as the 91.295-second run. Journal scan took 8.947 seconds,
forward validation 6.989 seconds, reverse validation 9.179 seconds, and
catalog selection plus live journal recovery 10.815 seconds. Both roots
validated and all 233 lookups passed, with all 32 dictionaries and 17 free
blocks. Keep both timings as the baseline for the DMA candidate.
See the [second cold-boot trace](../../docs/benchmarks/phase1-startup-profile-16mhz-cold-02.json).


The next timing candidate appends `phase1-boot-dma.conf` to the same left
profile build. During catalog validation it fills the existing 256-byte QSPI
bounce buffer with DMA and serves nearby reads from it. The build also uses
Zephyr's table-driven IEEE CRC32. It still runs the original integrity checks
and live journal recovery. In this build the trace's `dma_reads` count during
`dma_read_ahead` and validation is the number of physical page fills.

Compare request volume against the same precached-base fixture on the host:

```sh
c++ -std=c++20 -O2 -Itools/tests -Itools/storage \
  tools/bench/phase1_startup_reads.cc -o build/phase1-startup-reads
build/phase1-startup-reads build/phase1/loaded-fixture/simulated-pool.bin
```

The full-pool host open issued 974,018 direct requests for 22,137,506 bytes,
versus 213,430 DMA page fills for 54,638,080 bytes. Both selected generation 2,
recovered 17 tail entries, performed zero writes, and refused a corrupted
forward index. The request count and transferred bytes help size the next
hardware experiment. Neither measures latency, and the candidate still needs
a comparison on hardware. The 16 MHz candidate was then flashed and tested.
It reached ready in 41.4 seconds from USB after UF2, versus the 37.0-second
post-UF2 control. On a physical cold start, flash reported ready at 44.0 seconds
of device uptime, versus 37.4 and 91.3 seconds for the two 16 MHz controls. The
host USB-to-ready interval was 39.1 seconds. The candidate completed both boots
on the first open and recorded 213,443 DMA page fills each time. On the
physical run, the forward and reverse index passes took 23.5 seconds combined,
versus 16.2 seconds in the faster control. Both roots validated, and all 233
fixture lookups passed with 32 dictionaries loaded and 17 free blocks. The
candidate does not establish a boot-time improvement or meet the 10-second
full-pool target. See the [post-UF2 trace](../../docs/benchmarks/phase1-startup-profile-dma-ahead-after-uf2.json),
[cold trace](../../docs/benchmarks/phase1-startup-profile-dma-ahead-cold.json),
and [lookup results](../../docs/benchmarks/phase1-startup-profile-dma-ahead-lookups.json).






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
- The 32 MHz everyday build with matching-catalog validation reuse (`fe11123`)
  reached Javelin ready 38.8 seconds after its USB console appeared following
  a UF2 update. This single run reopened generation 2 with a full validation,
  32 dictionaries, 17 live tail entries, and 17 free blocks. The catalog matched
  the fixture byte for byte, and all 233 fixture lookups passed, including the
  full-stack misses. The board was left on this firmware with the pool intact.
- Using injected chords and USB capture, the 44-stroke prose burst passed
  at 349.90 WPM. The completed 300 WPM rerun processed all 1,400 strokes in
  order with zero input overflows. It missed one completion deadline and
  the host received one extra character.
