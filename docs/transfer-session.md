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
| `src/steno_squid/transfer_port.h` | Firmware glue policy (ownership, lock, source isolation, reboot-once) behind an `Env` template, host-tested |
| `src/steno_squid/transfer_staged_sink.h` | `Sink` adapter that attaches to an active staged update (NativeStage) |
| `tools/manager/browser/transfer_frame.js` | Browser mirror of the codec |
| `tools/tests/test_transfer_port.cc` | Glue policy tests with fake backend/env |
| `tools/tests/test_transfer_staged.{cc,py}` | Real `NativeStage` on simulated NOR, driven by the planner's real plans through the session |
| `tools/tests/test_transfer_session.{cc,py}` | C++ tests (golden vector built independently with zlib, 200 seeded lossy-link runs, mutation-checked) |
| `tools/manager/browser/test_transfer_frame.mjs` | Node test sharing the same golden vector |

## Frame (little-endian)

`0xB5 | type u8 | session u16 (nonzero) | offset u32 | length u16 | payload ≤ 240 | CRC-32 u32`

Magic `0xB5` is non-ASCII, so a binary frame cannot be mistaken for console
text. Overhead is 14 bytes (49 useful bytes per 63-byte HID report, 78%).
Types: BEGIN 1, DATA 2, END 3, COMMIT 4, ABORT 5; replies ACK 0x81, NAK 0x82,
ERROR 0x83, DONE 0x84. BEGIN payload: total u32, whole-payload CRC-32 u32,
max chunk u16, requested window u16 (capped at 16), kind u8, then up to 16 sink-defined argument bytes.

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

## Staged-writer adapter

The planning handshake stays on the legacy console: `native_install update begin <kind>` still activates the
staged update and returns the root and protected map the Manager plans against. The session then **attaches** to
that active update (BEGIN kind must equal the staged kind) and replaces only the slow `erase`/`write`/`commit`
round trips. No staging, allocation or on-flash change.

- Stream payload = records: `0x01 ERASE segment u16`, `0x02 WRITE address u32, length u16, data` (length <= 256,
  multiple of 4, 256-byte aligned; the staged writer enforces its own rules and any refusal fails the session).
- BEGIN args (12 bytes): commit segment, commit bytes, commit CRC-32 (what `commit SEGMENT SIZE CRC` carries today).
- `Verify` reads the commit payload back from flash in 4 KiB slices (NAK sink-busy until finished, so work stays
  bounded) and compares its CRC before `Commit` is ever attempted. `Commit` maps `CatalogResult` Busy to retry and
  Quarantined/refused to failure; `reopen_required()` tells firmware to run its existing reboot/reopen path.
- Abort releases the staged update only if this session attached to it.
- Firmware wrapper still owns: the connection-ownership check, `flash_lock`, input pause, USB-only restrictions for
  repack/recovery, upload status display, and the reboot after a successful commit.
- RAM: one 256-byte page buffer plus about 48 bytes of state, no heap. Erase of a 64 KiB segment is synchronous
  inside the record; its latency impact on the worker is unmeasured.

## Firmware wiring (opt-in)

`CONFIG_ZMK_STENO_SQUID_TRANSFER_SESSION` (Kconfig, default **n**; enable with
`config/boardsource-wireless-corne/transfer.conf` appended to `EXTRA_CONF_FILE`). With it off the build has no
transfer symbols and the console is byte-for-byte the same code path.

- **Routing** (`javelin_web_hid.cc`): a report whose first byte is `0xB5` goes to
  `steno_squid_native_transfer_report`. Console text and Base64 never start with that byte, so there is no mode
  switch. The host sends one frame per zero-padded 63-byte report; replies (at most 17 bytes) are packed into the
  reply report and sent on the same transport (USB or BLE session) the frame arrived on.
- **Capability**: `native_install update transfer-capabilities` answers `OK XFER1 49` only in this build; otherwise
  the console answers its usual unknown-command error and the Manager stays on Base64.
- **Policy in `transfer_port.h`**: the flash lock is taken without blocking (busy flash gives `NAK sink-busy`, the
  frame is not consumed); BEGIN attaches only to an update the **same connection** opened on the console; once a
  session exists, other connections get `ERR busy` and cannot drive or abort it; a stalled session expires on the
  next report; a successful or ambiguous commit triggers the console's reopen/reboot sequence exactly once.
- **Epoch guard**: `stage_epoch` increments whenever a staged update begins. A session whose update was lost
  (watchdog timeout, console `abort`) cannot abort or write into a newer update; its writes fail and its abort is a
  no-op. `owner_session` was promoted from a function static to a file variable so both paths share it.
- **Read-session reference count** (`dictionary_flash_read_session`) is decremented exactly once on every exit path.
- **Not changed**: staging, allocation, formats, the install watchdog, recovery/repack (`transfer`/provisioner) and
  the USB-only restrictions. The adapter can only attach to a staged update, never to the provisioner.

### Build results (Zephyr SDK 0.17.0, ZMK-pinned workspace, `blecorne_left`, `studio-rpc-usb-uart`)

| Build | FLASH | RAM |
| --- | ---: | ---: |
| Option off | 499,860 B (57.84%) | 233,414 B (89.04%) |
| Option on | 503,268 B (58.23%) | 234,326 B (89.39%) |
| Cost | +3,408 B | +912 B |

The off build adds only two `uint32_t` variables and a counter increment versus the previous commit. The on build
compiles without new warnings (the only warnings are in third-party CMSIS headers). `Verify` reuses the record page
buffer, so it adds no stack on the HID worker. Not run on hardware.
## Test status (2026-11-24)

Passed: C++ suites under clang++ and g++ (`-Wall -Wextra -Werror`), JS suite under Node 22.
The staged test uploads the planner's real import and compaction plans through the session into the real
`NativeStage` and requires flash **byte-identical** to the legacy erase/write/commit path, safe cold-open at all
100 simulated torn-write points per plan, and clean refusal for wrong CRC, truncation, disconnect and kind mismatch.
Six deliberate source mutations (three in the session, three in the adapter) are each caught. Not run: real firmware, USB/BLE,
browser WebHID, any hardware.

## Not yet done (needs approval or hardware)

1. Manager-side client: probe `transfer-capabilities`, build the record stream, window DATA frames, and fall back
   to Base64. Only the frame codec exists in JS today (`transfer_frame.js`).
2. CDC (Web Serial) mapping; this build carries frames on the HID console report only.
3. Hardware bring-up of the opt-in build (flash `Left.uf2` built with `transfer.conf`) once the board is free.
4. Instrumentation and throughput/RAM measurements (do not quote any figure
   until measured on the 64 MiB board once Phase 1 releases it).
5. Download (keyboard → host) direction; this prototype is upload only.
