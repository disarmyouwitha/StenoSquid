# Candidate B: first native-core QSPI diagnostic

Status: **two hardware runs passed on Boardsource left**, manual typing/display
smoke test pending. It is compiled only
with `CONFIG_ZMK_JAVELIN_DICTIONARY_BENCHMARK` in Left-Diagnostic. Left-Default and
the engine's current dictionary storage remain unchanged. No migration reader or
automatic new-store provisioning is introduced.

The USB-only command is `dictionary_storage_probe run`. It tests the actual
`NativeJournal`, ordered-index reader and reverse cursor against the external
Winbond chip. The adapter exposes only the existing reserved last 4 KiB scratch
sector at `0xFFF000`; native virtual segment 2 is translated into this sector.
It does not expose the rest of that virtual segment. Every read/program is bounded
to this sector. Its erase method erases exactly this one 4 KiB sector, not 64 KiB.

The handler holds the engine/flash lock for this synchronous diagnostic. **Do not
type while running it.** This is not a maintenance-scheduling or no-dropped-strokes
acceptance test. The runner times the command, but does not claim a thread CPU or
whole-translation latency result.

## Procedure

1. Refuse before writing if any scratch byte is nonblank, if the flash cannot be
   acquired, or the command syntax is wrong. No clear/force command is supplied.
2. Initialize the test epoch and append five committed records: short ASCII,
   multi-stroke UTF-8, maximum-length text, remove and reset. Page programs use
   aligned DMA bounce buffers and FF padding within the record's allocated span.
3. Reopen the segment and incrementally validate all five sequences. Write an
   incomplete next header and verify recovery identifies a sealed tail.
4. Persist a three-entry forward index and a separate reverse index. Run 32
   forward lookups through real QSPI XIP reads, using the existing Nordic-safe
   word loader. Compare the UTF-8 and 2,047-byte translation through fixed chunks,
   then seek/page the matching reverse result.
5. On success, erase the scratch sector and verify all 4,096 bytes FF. On failure,
   retain test bytes for investigation; another run refuses nonblank scratch.
   An interrupted command can also leave scratch nonblank. It cannot format a
   working dictionary region, and it does not automatically clear unknown bytes.

The host runner compares KAT lookup and personal export before/after, saves a
local personal backup beside its report, and requires both test and cleanup pass.

```sh
python3 tools/bench/run_storage_probe.py --serial USB_SERIAL \
  --output build/storage-probe/hardware.json
```

`USB_SERIAL` identifies the keyboard and is not a serial-port path. Transport is
USB HID. Close any other Javelin/Studio connection before running. The private
`.personal-backup.txt` file is not a public test artifact. Do not publish personal
dictionary contents or device serial numbers with measurement evidence.

## Evidence limits

The native handler test uses the actual implementation with strict scratch-sector,
page, DMA alignment and NOR programming doubles. It covers refusal before writes,
successful core/XIP/reverse operations, cleanup, program failure and safe refusal
on a subsequent run. CI still runs native recovery/fault tests separately.

The installed Boardsource CI diagnostic uses **521,432 bytes** of internal flash
and **224,926 bytes** of RAM (85.80% of 256 KiB). This includes unrelated diagnostics
and battery logging, not just the new core. It is not a Left-Default size increase.

## Hardware results — 2026-11-18

The exact `Boardsource-Left-Diagnostic` UF2 from
[CI run 44324302402](https://github.com/disarmyouwitha/StenoSquid/actions/runs/44324302402)
at `4661404` was flashed onto the identified Boardsource left. The UF2 SHA-256 is
`d1b99cef3384d8c59ea49aab3f75841fa5db0e7906cf581d454ba3253f8876be`.
The board reported Winbond `EF 40 18` and 2 MHz QSPI.

Both runs passed all five records, sequence recovery, sealed-tail detection,
ordered XIP lookup, UTF-8/maximum-length text comparison, reverse seek/page and
full scratch cleanup. Total command durations were **149 ms** and **148 ms**,
including both scratch erases. The runner verified unchanged KAT lookup and
personal export before/after each run. Separate full readback CRC checks passed
for Lapwing `00f2d636`, Plover `3e8f8d88` and extras `98af1ecc`. Personal storage
reported zero integrity errors. HID stack inspection after the test reported
2,660 bytes unused at its observed watermark; this is not a worst-case guarantee.

Forward lookup batches contained 32 samples each, with **92–214 µs** observed
over both runs. This probe uses `k_cycle_get_32()` at **32,768 Hz**, converted by
`k_cyc_to_us_ceil32`: resolution is approximately **30.5 µs**, much coarser than
the separate Phase-0 DWT timer described by `dictionary_benchmark meta`.
These are tiny three-entry index results, not large-overlay latency or meaningful
production p99 evidence. Use DWT for the subsequent performance diagnostic.

The [sanitized report](benchmarks/candidate-b-native-scratch.json) retains both
raw sample batches, artifact provenance, preservation checks and timing limits.
Personal backups, unique device serials and local flash manifest remain ignored.

This is a five-record/three-index-entry probe. It does not establish large-store
latency, the proposed capacity, native catalog publication, full engine adapters,
incremental cleanup, typing under maintenance, sleep/wake recovery or physical
power-cut safety. Those remain required before switching production storage.
