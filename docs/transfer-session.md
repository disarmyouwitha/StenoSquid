# Experimental binary transfer session (Phase 2 prototype)

Status: **host-tested prototype, not wired into firmware.** The legacy Base64
console is unchanged and remains the compatibility/recovery path. Nothing here
changes storage formats, shipping defaults, USB/BLE descriptors or the safety
queue. Throughput figures are **not measured**; see "Not yet done".

## Files

| File | Role |
| --- | --- |
| `src/steno_squid/transfer_frame.h` | Frame codec, CRC-32, resynchronising decoder (header-only, no Zephyr/heap) |
| `src/steno_squid/transfer_session.h` | Upload receiver state machine over an abstract `Sink` |
| `tools/manager/browser/transfer_frame.js` | Browser mirror of the codec |
| `tools/tests/test_transfer_session.{cc,py}` | C++ tests (golden vector built independently with zlib, 200 seeded lossy-link runs, mutation-checked) |
| `tools/manager/browser/test_transfer_frame.mjs` | Node test sharing the same golden vector |

## Frame (little-endian)

`0xB5 | type u8 | session u16 (nonzero) | offset u32 | length u16 | payload ≤ 240 | CRC-32 u32`

Magic `0xB5` is non-ASCII, so a binary frame cannot be mistaken for console
text. Overhead is 14 bytes (49 useful bytes per 63-byte HID report, 78%).
Types: BEGIN 1, DATA 2, END 3, COMMIT 4, ABORT 5; replies ACK 0x81, NAK 0x82,
ERROR 0x83, DONE 0x84. BEGIN payload: total u32, whole-payload CRC-32 u32,
max chunk u16, requested window u16 (capped at 16), kind u8.

## Success ladder (never skipped)

`ACK(Open)` sink accepted BEGIN → `ACK(Receiving)` each DATA accepted by the
sink (reply offset = next expected byte) → `ACK(Verified)` after END with
matching length, whole-payload CRC and `Sink::Verify` → `DONE` only after
COMMIT and `Sink::Commit`. These map to the plan's distinct stages: queue
acceptance, sink write, verification, durable commit. Transport/endpoint
completion and Manager reception are tracked outside this core.

## Rules

- One session at a time. A different BEGIN while active returns `ERR_BUSY`; no
  silent switching. A byte-identical BEGIN before any data is re-ACKed.
- DATA must land exactly on the next expected offset. Duplicates are re-ACKed
  without touching the sink, gaps get `NAK(out-of-order, expected)`, partial
  overlaps get `NAK(bad-offset)`. Streaming only; no whole-image buffering.
- Backpressure: a sink returning `kBusy` yields `NAK(sink-busy)` and leaves the
  session unchanged, so the host retries the same frame.
- Malformed lengths (> chunk, past total, bad BEGIN parameters) and CRC
  mismatch fail the session. Every failure, host ABORT, idle timeout and
  disconnect calls `Sink::Abort` exactly once. A finished result (COMMIT) is
  never rewritten by a later ABORT, timeout or disconnect.
- Timeouts use wrap-safe unsigned arithmetic (`Tick`).
- Resume: **not supported.** After any failure the host restarts with a new
  BEGIN. Resume would need staged-writer support and is a separate decision.

## Test status (2026-11-24)

Passed: C++ suite under clang++ and g++ (`-Wall -Wextra -Werror`), JS suite
under Node 22. Three deliberate source mutations (CRC check removed, ordering
check removed, abort removed) are each caught. Not run: real firmware, USB/BLE,
browser WebHID, any hardware.

## Not yet done (needs approval or hardware)

1. `Sink` adapter over the existing staged writer and catalog publication.
2. Wire mapping onto the 63-byte report (a negotiated mode switch on the
   existing console, since legacy parsing treats reports as text) and onto CDC.
3. Capability negotiation command and Manager fallback logic.
4. Instrumentation and throughput/RAM measurements (do not quote any figure
   until measured on the 64 MiB board once Phase 1 releases it).
5. Download (keyboard → host) direction; this prototype is upload only.
