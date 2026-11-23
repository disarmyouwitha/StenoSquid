# Firmware review: prioritized fix list

Reviewed 2026-11-23 against `main` at `ab9afbb`. **Review findings only; nothing has been changed, built or flashed.**
Line numbers are from that commit; recheck before editing.

How to read the tags:

- **[V]** I (the lead reviewer) checked the claim against the code.
- **[R]** A reviewer reproduced it by running a program.
- **[C]** A reviewer read the code and claims it; not independently checked.

The Javelin engine, Zephyr and part of ZMK were not available, so anything depending on them is listed under *Open questions* instead of being asserted.

**Constraints that still apply:** do not shrink input queues, safety buffers or stack margins to make room for anything. Do not change persistent storage formats without a stated migration decision. Every change that touches flash ordering needs a torn-write test that fails without the fix.

---

## Tier 1 — data safety and security (do first)

### F1. Torn erase of the older catalog slot can leave the store read-only [R]
- **Where:** `tools/storage/native_catalog.h` — erase of the destination slot (~353–363), `Inspect` checks only the commit word (~146), any `Corrupt` other slot means quarantine (~281–288), `Publish` refuses a quarantined `current` (~304).
- **Problem:** the slot being erased still holds a committed older root. If the cut lands after the header's first bytes are cleared but before the commit word (bytes 28–31), `Inspect` returns `Corrupt` instead of `Empty`. Reviewer's program publishes twice, cuts the erase of the third publish, reopens: tear offsets 1, 8, 27, 28 gave `Open=Quarantined` and the next `Publish` returned `Invalid`; offsets 0, 31, 32, 100, 127, 4095 were fine. Every checkpoint, rollover, mirror, stage commit, `Clear` and `Enable` erases an occupied slot, so this window is on every maintenance publish.
- **Fix:** before erasing the destination, program its commit word (bytes 28–31) to 0 (a legal 1→0 program) so an interrupted erase reads as `Empty`. Quarantine only when the corrupt slot's generation could exceed the valid one's.
- **Tests:** extend `test_native_catalog.cc` (currently cuts only the first two publishes, where the destination is blank) to cut the erase of an occupied alternate slot at every tear offset. Replace tests that accept `Quarantined` as a pass (`test_native_reclamation.cc:171–186`, `test_native_stage.cc:75`) with assertions that the store recovers and the next publish succeeds. Add bit-level and suffix tears to the `Nor` model (`native_test_nor.h` only clears byte prefixes).
- **Caveat:** uses the repo's own tear model. Real flash may tear differently; the fix is cheap and safe either way. Size: small.

### F2. The 3-segment reserve cannot be spent, so delete/rename/uninstall can become impossible near full [R, medium confidence]
- **Where:** `native_allocator.h:39` (`count > free-3`), `native_stage.h:569` and `:598` (`remaining <= 3` refuses), `native_store.h:97–98` (`NoSpace` before mirroring).
- **Problem:** every metadata-only update needs one erased 64 KiB segment for its replacement catalog payload. With 3 or fewer free segments, `stage.Begin(Rename)` succeeds but `stage.Erase(free segment)` is refused, and checkpoints return `NoSpace`. The store can't free space through the normal path and needs destructive recovery. This hits the factory-uninstall feature.
- **Fix:** let metadata-only kinds (Rename, Delete, Reorder, FactoryMember uninstall, SnapshotDelete) use the reserve for their payload segment. Let `Prepare` mirror when staging can't fit.
- **Tests:** near-floor fixture with free = 3: Rename, Delete and uninstall each complete and leave ≥ 3 free afterwards. Size: medium.

### F3. Bluetooth can reach whole-pool erase and other destructive operations [V]
- **Where:** `src/steno_squid/javelin_web_hid.cc:142–153` blocks only `update repack-capabilities`, `update recovery-capabilities`, `begin REPACK`, `begin RECOVERY`, `match RECOVERY` over BLE. `native_store_runtime.cc:1206–1244` (`begin CLEAR <crc>` erases and rewrites the pool) and `update begin base|delete|factory-member`, `reset_user_dictionary`, `user_dictionary_rebuild` are reachable. `console_commands.def` marks `usb_only` only for benchmark commands.
- **Authorization today:** an encrypted link at `BT_SECURITY_L2` from the bonded active-profile peer (`javelin_ble_hid.c:24–30`). Just Works pairing is not MITM-resistant.
- **Fix:** make `begin CLEAR`, `update begin base|delete|factory-member` and `reset_user_dictionary` USB-only, or require a physical on-device confirmation (key chord or display prompt) for BLE. Make the allowlist table-driven so each command and sub-operation carries a `usb_only` flag.
- **Tests:** host dispatch test: every destructive command returns an error over a BLE writer and succeeds over USB; small edits still work over BLE.
- **Why now:** the new BLE GATT service will inherit this policy. Size: small–medium.

### F4. Engine lock is held across USB/BLE reply waits [V]
- **Where:** `src/engine_init.cc:404–425` holds `JavelinEngineLock` for all of `Console::HandleInput`. `HidWriter::Flush` (`javelin_web_hid.cc:91–112`) blocks per 63-byte report (`k_sem_take(transmit_ready, 2 s)` on USB; `javelin_ble_tx.c:59` waits up to 2 s on BLE).
- **Problem:** `print_dictionary` and other long replies keep `flash_lock` for the whole transfer. A stalled host adds up to 2 s per failed report. Meanwhile the key path times out at 10 ms and re-queues; the 512-event queue can fill and overflow, which discards the chord. The comment at `javelin_ble_tx.c:1` and the claim at `native_engine_backend.h:1–2` that exports use independent snapshots don't match this.
- **Fix:** stream exports without `flash_lock`: hold a pinned snapshot (`ExportGuard` plus a read session already exist), take the lock per record batch and write replies outside it. Fix the two comments.
- **Tests:** a test writer that stalls transmit; assert the key path stays within its bound and queue occupancy stays well under 512. Record worst-case lock-hold time.
- **Rule for the new transfer service:** never wait on the radio or USB while holding this lock. Size: medium.

---

## Tier 2 — flash lifecycle and recovery

### F5. A late wake request can leave status stuck at "loading" [V code reading; not reproduced]
- **Where:** `dictionary_flash_pm.c:19–33` (`lf_request_restart` clears `lf_checked` without checking `lf_suspended`), `dictionary_flash.c:96` (worker drops the request when not suspended and nothing sets `lf_checked`), `flash_access.h:57`.
- **Effect:** `dictionary_flash_status()` stays LOADING, `try_service` returns false so the browser editor sees "Native flash busy", until the next idle power-down and wake. Window is narrow (two threads waking at once).
- **Fix:** have `lf_request_restart` return early unless `lf_suspended` under `flash_lock`, or have the worker set `lf_checked` when it drops a stale request.

### F6. Idle power-down also runs on a flash that failed to map [V]
- **Where:** `dictionary_flash.c:107–115` (`(!lf_mapped_ready || !lf_held)`), `:34–39`.
- **Effect:** a blank, unprovisioned or faulty flash cycles the rail about every 30 s plus bring-up time even on the steno layer (battery cost); status flips between READY and UNAVAILABLE.
- **Fix:** skip idle-off when mapping failed and the last open failed, or report UNAVAILABLE in that state.

### F7. No minimum off-time between power-down and the next power-up [C]
- **Where:** `dictionary_flash_qspi.c:22–29` and `:103`, `dictionary_flash_power.c:21–37`.
- **Problem:** only the retry path waits 500 ms; a wake right after power-down re-applies power to a chip that never reset.
- **Fix:** record the power-off time and sleep out the remainder of about 500 ms in `start_qspi`. Needs scope data on the real rail before picking the number.

### F8. `dictionary_flash_try_service()` ignores `lf_suspended` [C, low confidence]
- **Where:** `flash_access.h:47–61`. After a ZMK suspend `lf_checked` is still 1 and `lf_idle_off` is 0, so it returns true with the rail off; some paths in `native_store_runtime.cc` (e.g. ~1043) gate only on `healthy`.
- **Fix:** return false when `lf_suspended` and request a restart if idle-off. Confirm with ZMK whether it sleeps while USB-powered.

### F9. Fault and abort paths depend on an idle power cycle that typing postpones [C]
- **Where:** `native_store_runtime.cc:150–152` (`BaseProvider::Fault`), `:1240` (`begin CLEAR/RECOVERY`), `:1248–1251` (`abort`), `:911–917` (`install_timeout`), `:761–762` (`ForgetBaseTrust` zeroes RAM before the delete), `:1347` (scrub mismatch returns without resetting `scrub_offset/scrub_crc/scrub_cell`).
- **Problems:**
  - Reopen happens only through idle power-down and wake, and `acquire` calls `lf_note_use()`, so continued typing keeps deferring it; with `FLASH_IDLE_OFF=n` recovery needs a reboot.
  - `Fault()` does a `settings_delete` (internal flash write) that can run on the system work queue.
  - A failed trust-record delete is ignored; the scrub is then the only backstop, and it never extends `lf_last_use`.
  - After a scrub mismatch the stale scrub state can fault the store again on every reopen.
- **Fix:** post a rate-limited `lf_request_restart` from `Fault()` and install abort/timeout. Move the settings delete to the engine queue. On delete failure set a RAM flag forcing a full check on next open. Reset scrub state on fault and on `steno_squid_native_flash_open`.

### F10. Maintenance retry loop and install lease [C]
- **Where:** `native_store_runtime.cc:314–319` (`StepWork` retries every 2 ms with no cap while `healthy` but the lock can't be taken; `FinishMaintenance()`/`Kick()` flip `maintenance_hold` and the read session without `flash_lock`), `:298` (`IdleMaintenance` returns without re-arming), `:1306–1314` (an install lease whose BLE owner has gone is held until the 60 s timeout; `native_entry` already checks `steno_squid_javelin_ble_session_valid`).
- **Fix:** back off with a retry cap; do hold/release under the lock or atomically; reuse the session-valid check in the install path.

---

## Tier 3 — configuration, RAM and CI

### F11. Debug options ship in everyday builds [V]
- `config/nicenano-wireless-corne/corne_left.conf:36–37`: `CONFIG_STACK_SENTINEL=y` and `CONFIG_THREAD_NAME=y` apply to both boards' Default Left builds. Real RAM on a board at about 88.7% SRAM; byte cost of thread names depends on `THREAD_MAX_NAME_LEN`.
- **Fix:** move both to the diagnostic conf. **First** measure stack high-water marks (F13) so the sentinel isn't the only overflow detector you rely on.

### F12. Unenforced configuration dependencies [C]
- Add `BUILD_ASSERT(CONFIG_BT_L2CAP_TX_MTU >= JAVELIN_HID_REPORT_BYTES + 3)` and the RX-side equivalent (`javelin_ble_hid.c`); today they live only in a conf file.
- `ZMK_JAVELIN_STENO_BROWSER_DICTIONARY` binds `"HID_2"`, which exists only if `ZMK_HID_IO_PLOVER_HID=y` — `select` it or assert it. `ZMK_JAVELIN_STENO` `select`s `…EXTERNAL_FLASH`, bypassing its `depends on`.
- Split run-queue size: nothing enforces `CONFIG_ZMK_SPLIT_BLE_CENTRAL_SPLIT_RUN_QUEUE_SIZE >= 2*STENO_SQUID_PRACTICE_FRAME_PARTS + 6` under Practice (`split_status_central.c`); the Kconfig `default 40 if PRACTICE` may lose to ZMK's earlier default and only `corne_left.conf` sets 40.

### F13. RAM baseline and safe savings (needed before the BLE service)
Biggest static consumers found [C]: heap pool 32 KiB (`corne_left.conf:33`; what needs it is unmeasured), engine work-queue stack 8 KiB, main/system-work-queue/HID stacks 4 KiB each, flash worker stack 3 KiB, BLE TX stack 2 KiB, HID report queue ~2.9 KiB, `export_text` 2 KiB, stroke-tape buffer 1 KiB, `cells[]` (up to 16 entries, sizes estimated, `NATIVE_DICTIONARIES` allows up to 64), four `LiveView`s of ~1.5 KiB each.
- **Measure first (hardware):** `k_thread_stack_space_get` high-water marks on the flash worker during a cold Native open (it runs catalog validation and settings I/O on 3 KiB), on the HID worker during a staged commit (`ProbeStagedBase` puts a whole `Cell` on its 4 KiB stack), and heap peak under import plus Bluetooth plus typing.
- **Likely savings, low risk:** share one guarded scratch union for `entry_chunk[512]`, `incoming[512]`, `scrub_chunk[256]`, `export_text[2048]` and the provisioner's `readback[512]` (they are never used at the same time; roughly 1.3–1.8 KiB, estimate); make `iter_f/iter_r` sinks optional (~600 B).
- **Bluetooth buffers:** current `BT_L2CAP_TX_MTU=80`, `BT_BUF_ACL_RX_SIZE=84`. MTU 247 with data length 251 raises the shared pool (also used by the split link); estimate +2–4 KiB, but several counts are not visible — measure a real build. Compare MTU 80 / ~160 / 247 before paying for 247.
- **Gate:** add a minimum free-RAM check to CI (F14).

### F14. CI does not check what ships [C]
- Add a `.config` assertion script for Default builds: diagnostics, benchmark, serial console and battery log off; browser dictionary, BLE HID, flash idle-off and USB HID count on.
- Add a free-RAM threshold (for example 2 KiB) instead of the informational memory step.
- Require the release tag to be on `main`.
- Save the west cache from more than one matrix job; add auto-discovery for the sanitizer test list.

---

## Tier 4 — storage logic edge cases

| ID | Where | Problem | Fix |
|---|---|---|---|
| F15 | `native_store.h:167`, `:196–201`, `:185`, `:213` [C] | `Clear`/`Enable` don't check factory-absent rows (`row[11]`); preflight fails with no write, but the store closes (`opened=false`, `Invalidate`) | Reject `row[11]!=0`; invalidate only on `IoError` or post-erase failure |
| F16 | `native_stage.h:431–437`, `:446` [C] | Factory restore copies the candidate's pin over the retained one and never compares; any valid JSC4 image can be restored under a factory ID and inherit its overlay (the Python test may enforce this host-side — unverified) | Require `new_row[64..95] == retained pin` on restore |
| F17 | `native_stage.h:623–665` [C] | `Commit` doesn't check `store.opened` or `CanWrite`; a failed mirror is reported `Quarantined` though the new root is durable | Check at the start of `Commit`; report mirror failure accurately |
| F18 | `native_catalog.h:13–23`, `:220` [C] | Names for IDs 2–7 are hard-coded but `next_id` is only required `> id`; a catalog with `next_id < 8` could allocate a factory ID to a named import | Require `next_id >= 8` at provisioning and in `ValidatePayload` |
| F19 | `native_store.h:145` [C] | After one `NoSpace`, every edit needing a checkpoint returns `ReadOnly` until `Open` | Clear `last` after any successful stage, clear or enable, or retry on a timer |
| F20 | `native_live_journal.h:286–287`, `native_validator.h:65–67` [C] | Older-root fallback is usually unusable because the tail holds only 256 edits (up to ~512 behind) | Serve fallback reads with a larger read-only tail, or document it as best-effort |
| F21 | `native_rollover_job.h:108`, `native_store.h:171–186` [C] | `journal.Initialize` is one blocking 64 KiB erase; `Store::Clear` replays all journals in one call under the lock | Slice them like compaction |
| F22 | `native_catalog.h:50`, `:103/109` [C] | 4 pins, and `next_token == UINT32_MAX` disables pins silently until reboot | Surface the condition |

---

## Tier 5 — Practice, split link, display

| ID | Where | Problem | Fix |
|---|---|---|---|
| F23 | `practice_progress.c:118–168`, `:224–236`; `practice_state.h:183–185`, `:300`, `:315` [C] | The settings save (19 block CRCs, up to ~11 NVS writes, possible garbage-collection erase) is submitted before `practice_publish()` on the system work queue, so the DONE screen waits behind flash; also runs on every drill switch/layer exit | `k_work_reschedule` 0.5–1 s later, submit after the publish, or use a low-priority queue |
| F24 | `practice_progress.c:149`, `:166–167` [C] | A failed `settings_save_one` isn't retried until the next checkpoint | Resubmit after a delay on error |
| F25 | `split_status_central.c:182–199`; `practice_view.h:157`; `status_receive.h:13–17` [C] | Success means "queued, not delivered"; if the right half missed a full text frame it ignores every following state-only frame until the 60 s heartbeat | Send full frames for the first N frames after a text change; shorten the heartbeat in Practice to ~10 s |
| F26 | `status_receive.h:62`; `split_status_display.c:98–108` [C] | One refresh per tape message: up to 3 full-screen redraws per stroke, partial text may flicker | Refresh on the last expected fragment or after a 20–30 ms delayed work item |
| F27 | `split_status_central.c:301–320` [C] | Heartbeat is unconditional: forever, even idle/asleep, forces a full Practice frame | Stop when not ACTIVE or lengthen |
| F28 | `practice_model.h:97–128`; `practice_state.h:247–250` [C] | Deck build on the key thread under three locks: ~4,500 weight evaluations (estimated 4–7 ms) | Compute the 300 weights once per deck |
| F29 | `practice_engine.cc:63–65` [C] | `set_active(false)` silently does nothing if the engine lock fails and isn't retried | Retry from `flash_ready_work` |
| F30 | `practice_state.h:280–281` [C] | Replay time vs real uptime can give negative `elapsed`; the cast wraps and the answer is silently excluded | Treat negative as untimed explicitly |
| F31 | `split_status_display.c:59–67`, `:98` [C] | `memcmp` on a struct with uninitialised padding can fire spurious redraws | `memset` first or compare fields |
| F32 | `practice_state.h:278` [C] | `practice_correct` does dictionary lookups (flash reads) while holding `practice_mutex` | Look up before taking the mutex |
| F33 | `native_store_runtime.cc:886–898` [C] | Base-hit path does a byte-wise XIP `strlen`, a `malloc`, then a second copy; OOM returns "not found" with no counter | Scan by words; count malloc failures in `user_dictionary_status`; size the heap from measurement |
| F34 | `engine_init.cc:210–213`; `ReplyEdit` [C] | Distinct failures (input pending, exporting, flash error, NoSpace, ReadOnly, Invalid) all map to one message | Return and map the real result code |

---

## Cleanup (no behaviour change)

- `Kconfig`: `ZMK_STENO_SQUID_NATIVE_STORE def_bool ZMK_JAVELIN_STENO_EXTERNAL_FLASH` is always on, so the `#else` branches in `engine_init.cc:305–317`, `zmk_clock_shim.cc`, `practice_state.h` and `stroke_feed.cc` are dead.
- `native_store_runtime.cc:435` calls `steno_squid_javelin_response_active()`, defined only with `BROWSER_DICTIONARY`; an external-flash build without it would not link.
- No callers (grep whole repo): `extras_dictionary_name`, `extra_names[]`, `EXTRAS_*` enum, `NativeInputQueue::Cancellations()`, `Tail::Append` (tests only), hash `Find`/`FindLatest`/`kMaxCollisions` (probe only), `binary_record.h` `MatchOutline`/`CompareOutline(address…)`. `lapwing_dictionary()`, `plover_dictionary()`, `extras_dictionary(i)`, `steno_squid_user_dictionary_open()` are used only by the diagnostic benchmark and a test stub and hard-code old identities.
- Dead record fields `magic`, `pool`, `crc` in `practice_model.h:23–30` (persistence now uses `progress_header`; stale `STP1` comment).
- `dictionary_flash_scrub.c` is an 11-line pass-through. `xip_load.h:11` hard-codes `0x12000000`. `CMakeLists.txt` has a dead `JAVELIN_SRC_DIR` `javelin/` fallback and a redundant split-queue default.
- Unused diagnostic counters (`DIAG_QUICK_FALLBACK`, `DIAG_ERASE`, `DIAG_PROGRAM`, `DIAG_QUICK_CHECK`), "serial erases/writes" labels, and an unreachable `UNAVAILABLE : LOADING` branch in `dictionary_flash.c:41`.
- Stale comments: `packed_index.h:1` ("not linked into firmware"), `Kconfig` stroke-tape help (MORE is sent above 3 characters; HINT is a separate write), `FLASH_IDLE_OFF` help ("quick check"), `dictionary_flash_internal.h` serial-poll comment, `docs/archive/hybrid-native-catalog-checkpoint.md` (twelve sectors at `0xFF4000`, not seven at `0xFF9000`), and `screen.h`.
- Duplicated logic: font-fit helpers (`screen.c` vs `practice_widget.c`, which fit twice per update), hard-coded 66/68 widths where `STENO_SQUID_TEXT_WIDTH` exists, `"%u/10"` where `STENO_SQUID_WORD_ROUNDS` exists, receiver caps (`xp_earned > 120`, `first_try > 10`) with no `BUILD_ASSERT` tying them to the model's reward constants (a change would silently make the right half drop every frame), extent and dictionary row decoding with magic offsets (96/24) in at least 8 files, and `FactoryName` in the catalog core (layering leak).

---

## Test gaps to close alongside the fixes

1. Cut on the erase of an occupied alternate catalog slot (F1), with bit-level and suffix tears.
2. Anything near the 3-segment floor (F2), and leaving `Quarantined`.
3. `Clear`/`Enable` on a factory-absent row (F15).
4. Fallback to a root more than 256 edits behind (F20).
5. Central `send_status` policy: `text_changed`, `have_text`, retry, `force_full_status`, heartbeat backoff, and a stalled link (F25, F27).
6. `status_receive.h` tape and status `clear_tape` rules.
7. Firmware-level power-management state machine: `lf_request_restart` states, idle-off conditions, bring-up retry and polarity, trust record (F5–F9). `test_flash_access.py` covers only acquire/wake with stubs.
8. BLE dispatch of every destructive command (F3).
9. Erase-count/wear assertions.

---

## Suggested order

1. **F1** — smallest change with the highest data-safety value; ship with its tests.
2. **F3** — before any BLE bulk work starts.
3. **F2**, then **F15–F17** (storage logic).
4. **F4**, designed together with the transfer-session lock rule.
5. **F5–F10** flash lifecycle, one PR with a state-machine test.
6. **F11–F14** config, RAM measurement and CI gates — measure before removing or sharing anything.
7. **F23–F25** Practice save ordering and frame resync, then the rest of Tier 5.
8. Cleanup last, as pure no-behaviour commits.

## Open questions (need ZMK/engine sources or hardware)

- Does `zmk_split_central_invoke_behavior` block, or drop the oldest queued message and return 0 when the run queue is full? (Drives F25 and F12.)
- Is kscan on the system work queue, and are right-half key events processed there? (Drives F23, F32 and how severe F4 is.)
- Does `zmk_split_peripheral_status_changed` fire on the right half in the pinned ZMK (`4dd95d78`)? If not, a stale sequence counter can ignore frames after a central reboot.
- Is `settings_load_subtree_direct()` legal inside the commit handler (`practice_progress.c:99`) while `settings_load` holds the settings lock?
- Engine `Console::BUFFER_SIZE` (needs about 730 characters for `native_install update write` lines), whether `new (definition) StenoCompactMapDictionary` allocates from the heap (a leak per staged commit if so), and which heap `malloc` uses.
- Real interrupted-erase behaviour on the W25Q128 (F1), rail fall time with and without the bleed resistor (F7), and measured stack and heap high-water marks (F13).
