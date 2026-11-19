# StenoSquid cleanup and engineering spec

> Historical record. For current storage behavior and open validation, see [Native dictionary storage](../native-engine-integration.md).

Status: **implemented and merged into main; CI and initial nice!nano hardware checks passed; extended acceptance pending.**
The personal-dictionary redesign is complete and its separate spec was retired.
P7, P8 and X1 are superseded. No migration code was added. The owner explicitly
approved resetting Practice XP/stats for P17's smaller block-based saves.
First-party code uses 0BSD; upstream dependencies retain their own licenses.

Current verification and intentional exceptions:

- Phases 0–4 and the test foundation passed every CI firmware/probe build and native test.
- The final code passes local left/right/diagnostic builds, native power-loss/storage
  tests, protocol tests and the real LVGL renderer. The final CI matrix is green.
- Both nice!nano halves are flashed. Typing, Javelin, all Practice drills and
  XP persistence after reboot passed on hardware; extended checks remain listed
  in cleanup-acceptance.md.
- P12 retains full heartbeat snapshots: without a return acknowledgement path, a
  hash-only heartbeat cannot repair a lost text frame. Unchanged snapshots no
  longer redraw the screen; ordinary Practice updates carry state without text.
- P5/P10 expose probe clock and rail-settle controls, but production timing stays
  at the measured defaults until PCB testing. No unmeasured speedup is claimed.
- D2/T8 bench tools remain available for first PCB bring-up as requested.
- T7's first-party tests now include private implementation headers. Only the
  pinned upstream ZMK layer test extracts functions, using a shared literal-aware
  parser rather than naïve brace counting.
- X2 (new fonts), X3 (pruning upstream compilation), X4's generated Windows table
  and X8 (cropped effect assets) remain optional follow-ups. Approved artwork and
  text output stay unchanged. X4's sortedness checks and X5 are implemented.
- The final generic-flash rename is a separate, behaviour-preserving commit.
- Cleanup is merged into main after CI and initial hardware checks. The published
  v0.1.1 release remains unchanged.

See [development checks](../development.md) and [cleanup acceptance](cleanup-acceptance.md).

Reviewed at commit `7908a68` (main), 2026-11-16.

## 1. Scope

A full read of the module (firmware under `src/`, `dts/`, `include/`, board
configs, `stenosquid.py`, `tools/`, CI) looking for three things: efficiency
to gain, better engineering, and cruft to remove.

**Goals**

- Fix the latent bugs the review found.
- Remove dead code, stale docs and leftover scaffolding.
- Collapse duplicated logic to one copy.
- Stop the keyboard's key-handling thread from stalling behind slow work.
- Cut install time, boot-check time, BLE traffic and flash wear where it is cheap to do so.
- Make CI faster and its inputs pinned.

**Non-goals**

- No new user-facing features.
- No change to the keymap, the dictionaries, or the on-flash personal dictionary format.
- No change to the Javelin fork except where an item says so.

**Ground rules**

- One phase per branch/PR, in the order below. Each phase builds and passes CI on its own.
- Phases 0–2 must not change behaviour, wire formats or stored data.
- Any item that changes a wire format (host ↔ left half, or left ↔ right half) is called out as **protocol change** and needs both ends flashed together.
- Settings keys already on users' keyboards (`lapwing/verified`, `lapwing/diag`, `steno_squid/practice/progress`, the Javelin selection keys) keep their names.

**Original review limitations (resolved during implementation)**

The original review had no engine/Zephyr checkout or C compiler. Implementation
uses both pinned dependencies, native C/C++ tests and real firmware builds. Items that depend on engine internals are marked
*(verify against fork)*. CI is the gate for every phase.

## 2. Decisions needed before work starts

**Decided 2026-11-16 — these override the "Recommendation" column below.**

| # | Decision | Effect on this spec |
|---|---|---|
| D1 | **Retire serial provisioning.** Keep serial diagnostics and the battery log. | S1 becomes "delete the serial copy"; S5 (separate serial command dispatcher) is required, not optional; X6 only needs to cover what is left (`Z`, `T`, `R`). Remove the serial upload path from `stenosquid.py`, its tests and the READMEs. |
| D2 | **Leave the bench tools (`R`, `T`, `tools/matrix`, `tools/qspi` variants) alone for now.** The owner removes them after the Boardsource board is wired and tested. | Do not delete or gate them. Still fix B14 (wrong pin text) since the rail test is about to be used on that board. T8 is on hold. |
| D3 | **Rename `lapwing_*` as a pure rename commit, last** (X7). | No deprecated Kconfig alias needed — single user. Stored settings keys still keep their names. |
| D4 | **Non-split boards are not supported.** The pretence is `CMakeLists.txt:48`, `if((NOT CONFIG_ZMK_SPLIT) OR CONFIG_ZMK_SPLIT_ROLE_CENTRAL)`, which builds the engine for a one-piece board that the rest of the module cannot drive. | Change the condition to `CONFIG_ZMK_SPLIT_ROLE_CENTRAL` only, share it with the `layer_control.c` block at `:17`, and make the Kconfig options depend on the central role. |
| D5 | **Both halves and the host tool are always updated together.** | Protocol changes are allowed without compatibility shims. A version field in the `status` reply and split status message is still cheap insurance but optional. |
| D6 | **Local `claude/*` branches deleted** (done 2026-11-16, all 14). | Nothing left to do. |

**Personal dictionary writes** are covered by a separate spec,
the completed personal-dictionary redesign, which replaces the
storage format (one 10.9375 MiB region of 64 KiB segments instead of two 4 MiB
snapshot slots) and the import protocol. Where the two specs touch:

- It supersedes P7, P8 and X1 here; skip those. Do not add the binary search
  to `EditSource::Prepare` — that code is deleted.
- It relies on P1 (bounded lock on the key path) and P3 (engine init on a work
  item) from this spec; do those first or together.
- S6 (one console command table) should include its two new commands.
- S16 / the dictionary manifest should carry its region constants
  (`0x500000`, 175 segments of 64 KiB) instead of `SLOT_BYTES`.
- T7's rewrite of `test_user_dictionary_snapshot.cc` is replaced by that
  spec's test list.
- The README's "~90,000 entries" and flash-map picture change there, not here.

Original options, for the record:

| # | Decision | Recommendation |
|---|---|---|
| D1 | **Serial provisioning** (`dictionary_flash_serial.c`) is called "legacy" in `logging.conf` and duplicates the HID installer. Keep it, or retire it and keep only the serial *diagnostics*? | Retire serial provisioning; keep diagnostics and battery log on a small serial command dispatcher. Removes ~200 lines and one of two copies of the erase/program/verify code. |
| D2 | **Bench tools**: the rail test (`R`), power-cycle bench (`T`) and `tools/matrix` were bring-up aids. `Kconfig` and `flash_diagnostics.h` themselves say to delete `flash_diagnostics.c` "once debugging is done". | Keep `S`/`D`/`Z` status and counters. Move `R`/`T` behind a bench-only option (default off). Delete `tools/matrix` unless it is still used for new builds. |
| D3 | **Rename `lapwing_*`** to something generic. The files handle Lapwing, Plover and extras; `dictionary_flash_mapped_address()` is used only as a boolean. Touches ~20 files, every test that slices source by name, and the user-visible Kconfig symbol `ZMK_JAVELIN_STENO_EXTERNAL_FLASH`. | Do it, but last (phase 5), as a pure rename commit. Keep the old Kconfig symbol as a deprecated alias for one release. |
| D4 | **Non-split (one-piece) boards**: `CMakeLists.txt` builds the engine for non-split, but `layer_control.c` builds only for a split central, so a one-piece build would never power the flash for the steno layer. No CI job builds it. | Drop the non-split branch from CMake and say so in the README, unless a one-piece board is planned. |
| D5 | **Protocol changes** (items marked so in phase 3) mean a left-half update must be paired with a new `stenosquid.py`, or with a right-half update. | Accept, and add a protocol version to the `status` reply and the split status message so a mismatch is reported instead of misread. |
| D6 | **Local branches**: 13 `claude/*` branches are fully merged into main; `claude/audit-instrumentation` is 4 "Temporary:" commits ahead (CI memory reports, since superseded by `tools/ci/memory_by_owner.py`). | Delete all 14. Not done without your say-so. |

## 3. Phase 0 — latent bugs (small, do first)

Each is a few lines. None changes behaviour on a healthy keyboard.

| ID | Where | Problem | Fix |
|---|---|---|---|
| B1 | `dictionary_flash_pm.c:69`, `dictionary_flash_serial.c:68,95,124` | `squid_qspi_reactivate()` and `crc32_ieee_update()` are called with no declaration in scope (no in-repo header these files include declares them). Builds only because the pinned GCC warns instead of erroring. | Include `w25q_quad.h` from `dictionary_flash_internal.h`; include `<zephyr/sys/crc.h>` in the serial file. |
| B2 | `engine_init.cc:296`, `:221-224`, `:275-277` | `initialized` is set before init succeeds. If the personal dictionary fails to open once (for example a QSPI hiccup), its console commands stay unregistered until reboot. | Register the commands always; open the store lazily on use. Delete the unreachable `entries.IsEmpty()` branch (`:239-242`). |
| B3 | `dictionary_flash_qspi.c:266` vs `dictionary_flash.c:150-153` | The "ready changed" hook fires before the caller clears `lf_idle_off`, so after a *failed* wake the display can be told READY and never corrected. | Move the hook call to after the state update (falls out of S4 below). |
| B4 | `dictionary_flash_provision.c:164-175` | A failed HID repair leaves the dictionaries unmapped but never fires the "ready changed" hook; the serial path does. | Fire the hook wherever `lf_mapped_ready` changes for good. |
| B5 | `dictionary_flash.c:132-133,148`, `dictionary_flash_provision.c:150` | If the post-install check fails, `lf_provisioning_ready` goes false and HID then refuses everything but `status`/`abort` — so the repair command is locked out on every boot. | Gate provisioning on "QSPI works", not "images verified". |
| B6 | `dictionary_flash_qspi.c:28-55`, `dictionary_flash.c:150,163` | A flash that fails bring-up (missing, blank, glitched) keeps its rail and QSPI powered until ZMK sleeps, and is never retried. | Power an unmapped flash down after a timeout; let hold/acquire request a rate-limited retry. |
| B7 | `dictionary_flash_power.c:23` vs `dictionary_flash.c:172` | Narrow race: a layer hold arriving between "unmapped" and "idle-off" requests nothing, so the first stroke pays the wake delay. | Set `lf_idle_off` first, or re-check `lf_held` after power-down. |
| B8 | `dictionary_flash_qspi.c:255` (also `:194,246`, `dictionary_flash_diag.c:55`) | Return value of `lf_clear_trust()` ignored after a failed full check; a stale "verified" record could let the next boot skip the check. | On failure, set a RAM flag that distrusts the stored record. |
| B9 | `Kconfig:75-77`, `javelin_web_hid.cc:15,17,58,343` | `ZMK_JAVELIN_STENO_BROWSER_DICTIONARY` uses USB HID unconditionally and binds the literal device `"HID_2"`, but does not depend on USB or on `USB_HID_DEVICE_COUNT >= 3`. A wrong config fails silently. | Add the Kconfig dependencies and a `BUILD_ASSERT` on the device count. |
| B10 | module `Kconfig:142-145`, `corne_left.conf:6` | Practice needs `ZMK_SPLIT_BLE_CENTRAL_SPLIT_RUN_QUEUE_SIZE=40` (ZMK default 5). Only the board conf sets it; with the default, frames drop and the key thread stalls 100 ms per overflow. | Set the default from the module Kconfig, or `BUILD_ASSERT` it. |
| B11 | `zmk_clock_shim.cc:6-8` | `GetMicroseconds()` converts a 32-bit cycle counter, so it jumps (rather than wraps cleanly) about every 36 hours of uptime. *(Engine's use of it: verify against fork.)* | `(uint32_t)k_ticks_to_us_floor64(k_uptime_ticks())`. |
| B12 | `compile_dict.py:481`; also `render_keymap.py:177,296`, `test_hint_index.py:31`, `test_practice_words.py:10` | Text files opened without `encoding="utf-8"`. On Windows `stenosquid.py --install` crashes compiling the dictionaries (reproduced here: `UnicodeDecodeError … cp1252`). CI hides it with `PYTHONUTF8=1`. | Pass the encoding everywhere; drop the env var from CI. |
| B13 | `render_keymap.py:15,295` | Still writes to `docs/keymap/` and rewrites README links matching that path; the images moved to `assets/keymap/`, so it silently updates nothing. | Point both at `assets/keymap`. |
| B14 | `dictionary_flash_diag.c:92-140`, `Kconfig:36-45` | Rail test prints "P0.31" and defaults the sense pin to 7 — both Boardsource values. On the nice!nano build the rail is P0.13 and chip select P0.17, so the test reads the wrong pin and gives wrong advice. | Print the pin from devicetree; derive the sense pin from the QSPI chip-select pin. (Skip if D2 removes the test.) |
| B15 | `practice_engine.cc:173-174`, `:235-237` | `practice_advance` does nothing (and does not reschedule) if it runs early; `practice_first_stroke_correct` subtracts pointers with no NULL check. Both are unreachable today but fragile. | Reschedule when early; guard the NULL. |
| B16 | `split_status_display.c:70` | Received layer-name bytes are masked to 7 bits but not checked printable, unlike tape and practice text. | Reject bytes outside 0x20–0x7e. |
| B17 | `import_personal.py:142-147`, `stenosquid.py:609` | A status reply missing a key raises an uncaught `KeyError` (traceback instead of a message). | Catch it at the top level. |

Doc fixes that ride along with phase 0:

- `config/boardsource-wireless-corne/README.md:28` links the deleted `../steno-squid-left/README.md`.
- `config/nicenano-wireless-corne/README.md:292` says running the uploader with only a port "checks for MAPPED"; it actually writes missing images. The read-only command is `--status`.
- `config/nicenano-wireless-corne/README.md:330` says the build "patches a private copy" of the segment builder; nothing in CMake patches anything.
- README mentions only `hidapi`; serial paths need `pyserial`, artwork needs Pillow.
- Stale code comments: `screen.h:38-40` (tape is above the pet, not below), `split_status_central.c:249` (there is a 60 s heartbeat), `practice_progress.c:21-23`, `Kconfig:126-129,143`, `dictionary_flash_internal.h:5-11,67`, `dictionary_flash_qspi.c:57-59,158`, `dictionary_flash.c:207`, `javelin_ble_tx.c:1`, and the two CMake comments at `CMakeLists.txt:280,290` that describe a two-library split that no longer exists.

## 4. Phase 1 — remove cruft (behaviour-preserving)

### Firmware

| ID | Remove | Evidence |
|---|---|---|
| C1 | Every `CONFIG_ZMK_SLEEP` guard in the flash stack (14 across 8 files) and the `IS_ENABLED(…EXTERNAL_LAPWING)` guards in `layer_control.c` | `EXTERNAL_LAPWING` depends on `ZMK_SLEEP` and CMake makes it mandatory, so the other branch can never build. Replace with one `BUILD_ASSERT`. |
| C2 | `CMakeLists.txt:65-69,72,318` redundant re-tests; `Kconfig:81` redundant `&& ZMK_SLEEP` | Same reason. Make `ZMK_JAVELIN_STENO` select the option instead of a CMake `FATAL_ERROR`. |
| C3 | CMake pass-through variables: `JAVELIN_SEGMENT_BUILDER_SRC`, `JAVELIN_NORMAL_MODE_SRC`, `JAVELIN_COMPACT_MAP_SRC`, `ZMK_SHIM_SRC`, `ZMK_INIT_SRC`, `ZMK_BRIDGE_SRC`, `JAVELIN_LIB_SRC`; the commented-out `fake_processor.cc` line; `*_BYTES` defined on two targets | Left over from when the build patched engine sources. Each is used once. |
| C4 | `steno_squid_practice_snapshot()` (`practice_engine.cc:270`, `split_status.h:24`) | No callers anywhere. |
| C5 | Dead writes on the Practice results screen (`practice_widget.c:321,373-374,397`) and unreachable `feedback_names[3]` | Overwritten by `progress_labels()` one line later. |
| C6 | `#include "processor/first_up.h"`; forward declarations in `zmk_platform_shim.h:15-16`; `STENO_S1` macro; no-op `behavior_javelin_steno_init` and `subscription_changed`; unread `bluetooth`/`session` fields of `transmit_report`; four `printk("[JAV] …")` lines | Repo-wide grep finds no use (the last `printk` duplicates a `LOG_INF`). |
| C7 | `#else return false` stubs for `plover_/extras_verify_native_dictionary`; double file-level `#if` gating in `flash_diagnostics.c`, `dictionary_flash_diag.c`, `dictionary_flash_serial.c`; `__has_include` fallback in `flash_diagnostics.c:21-26` | Callers are already inside the same `#if`; CMake already gates the files and always writes the header. |
| C8 | Pet art: `sleep` sprite is byte-identical to `blink`; `typing`, `jump`, `land`, `spark` are 1-pixel shifts of `idle`/`blink` (the generator builds them that way); `rising_7` is an all-zero frame; pass-through `ROW()` macro; duplicate size assert | Keep `idle` + `blink`, express the rest as y-offsets (the widget already offsets with `lv_obj_set_y`), allow a NULL "hide" frame. Saves ~1.6 KB and five descriptors. |
| C9 | `STENO_SQUID_PRACTICE_SECOND_ERROR` — sent and validated, never drawn. `record.rounds` — saved, never read. | **Choose**: draw "RETRY 2" and show rounds, or remove both. Removing `rounds` changes the stored record, so defer that half to P6. |

*Not removable without checking the fork:* `xip_load_u24` and
`lapwing_copy_lookup_text` have no callers in this repo but are almost certainly
called by the fork's `compact_map_dictionary.cc`. The ~60 engine files compiled
but probably unused here (HAL, split, gemini/tx_bolt/procat/passport processors)
can only be pruned by trial link; see X3.

### Tooling and repo

| ID | Remove | Evidence |
|---|---|---|
| C10 | `tools/tests/capture_javelin_logs.py` | Referenced nowhere; POSIX-only; not a test. |
| C11 | `compile_dict.py`: `--asm-output`, `--max-entries` and the docstring advertising them; unused `STENO_KEYS`, `IMPLICIT_HYPHEN`, `LEFT_BANK`, `RIGHT_BANK`, `VOWELS`, `popcount32`, `write_sized_list_ptr`, `Optional` | The internal-flash path they served was removed earlier. |
| C12 | `stenosquid.py:254-263` "alternate name" recompile | CI always compiles with `--name lapwing-base` and the firmware overrides the name. |
| C13 | `.gitignore`: `dicts_src/`, `dicts/*.bin`, `javelin/`. Add `javelin-steno/`, `zmk/` (created by `fetch_pinned.py`). `build.yml:9` ignores a `docs/**` path. | None of the removed paths exist. (This spec lives in `docs/`, so keep or drop that ignore line deliberately.) |
| C14 | `assets/keymap/plover-hid-*.svg` | Generated but not referenced by any Markdown file. Reference it in the README or stop rendering it. |
| C15 | `config/corne.keymap` `#ifndef` override layer | The only profile overrides 4 of 8 macros; the other 4 are never overridden. Inline into one keymap file and update `render_keymap.py`. |

## 5. Phase 2 — one copy of each thing (behaviour-preserving)

| ID | Duplication | Target shape |
|---|---|---|
| S1 | Erase loop, NOR preflight + page program + readback, verify-with-unmap, and the "invalidate trust" triple exist in both `dictionary_flash_serial.c` and `dictionary_flash_provision.c` | `lf_erase_image`, `lf_program_chunk`, `lf_verify_image_raw`, `lf_invalidate_trust` in one file; transports only parse. If D1 retires serial provisioning, this collapses to deleting one copy. |
| S2 | `wait_until_ready` ×4 (serial, provision, `user_dictionary_store.cc`, `tools/qspi/scratch.c`); `XIP_BASE` ×4; QSPI start-up sequence duplicated in `tools/qspi/src/main.c:56-113` | One `squid_qspi_wait_ready()` and one start-up routine in `w25q_quad.c`, which the probe already compiles. Make `xip_load.h` valid C so `dictionary_flash_qspi.c:71-75` stops hand-copying its erratum workaround. |
| S3 | Raw writes to GPREGRET2 in six places | Behind `lf_trust_*` helpers. |
| S4 | Post-bring-up bookkeeping ×3 and take-down ×2 (`dictionary_flash.c`, `_serial.c`, `_diag.c`, `_qspi.c`) | One `lf_bring_up()` / `lf_take_down()` pair. Fixes B3 as a side effect. |
| S5 | Diagnostics and battery log can only exist in a serial-provisioning build because the one command dispatcher lives in `dictionary_flash_serial.c` | Split out `serial_console.c`; each feature registers its commands. Lets a diagnostic build use the HID installer. |
| S6 | Console command whitelist kept in three places (`javelin_web_hid.cc:157-173`, `engine_init.cc:263-274`, `user_dictionary_store.cc:460-475`) | One table of `{name, handler}` used for both registration and the whitelist. |
| S7 | Hand-written `extern` declarations in five `.c`/`.cc` files | Move to `zmk_platform_shim.h` / `javelin_hid_transport.h` so the compiler checks signatures. |
| S8 | HID transport: channel-prefix parse ×2, error reply ×4, USB and BLE packetise loops near-identical | `parse_channel()`, `reply_error()`, one chunker with a send callback. |
| S9 | "First font that fits 66 px" implemented five times with five font lists and the width as `67`, `66` and eight bare literals | One `fit_label()` and one width constant. |
| S10 | Layer-name packing hand-rolled on both halves, not shared and not host-tested; split message type constants spread over three headers | One `split_protocol.h`; reuse the tape pack/unpack. |
| S11 | Practice `progress` word packed with magic shifts in two files; drill-field extraction repeated four times; fragment size `5` as a literal | Named shifts, one pack function, accessors. |
| S12 | Table sizes (`STENO_SQUID_WORD_COUNT 300`, `MULTI_COUNT 20`, `BUILD_COUNT 10`) are free-standing macros; dictionary selection bits are magic `1/2/4` with no headroom check | `BUILD_ASSERT(ARRAY_SIZE(...) == COUNT)`; named bit constants with a `static_assert` on the shift. |
| S13 | `zrst` behavior node declared in both right-half overlays; nice!nano QSPI pin map lives in `src/steno_squid/qspi.overlay` while Boardsource's is in `config/` | One `dts/behaviors/steno_squid_status.dtsi`; move the pin map to `config/nicenano-wireless-corne/qspi.dtsi`. |
| S14 | Engine and first-party code share one library, so `-fpermissive -Wno-invalid-constexpr` also apply to `engine_init.cc`, `user_dictionary_store.cc`, `javelin_web_hid.cc`, `practice_engine.cc`, `stroke_feed.cc` | Two libraries; permissive flags on the engine only. Fix whatever first-party warnings appear. |
| S15 | Python: JSC4 reader ×2; download + sha256 + compile + verify ×3; serial line reader ×3; port discovery ×2 (and inconsistent: `port.product` vs `port.description`); C-source function extractor ×4; Zephyr stub shims rewritten in six tests | `tools/dictionaries/manifest.py` (URLs, hashes, XIP offsets, names), one `jsc4.py`, one `tools/tests/hostlib.py` (compiler discovery honouring `CC`/`CXX`, shared stub header). |
| S16 | Constants shared by Python and C with nothing tying them together: XIP offsets (`0x12000000/200000/500000`), chunk size 512, HID report size 63 and usage page, snapshot limits, serial command letters | A host test that parses the C headers and compares to the manifest. |

## 6. Phase 3 — responsiveness and efficiency

Ordered by benefit. P1–P3 are the ones a user can feel.

### Key handling must not stall

| ID | Problem | Change |
|---|---|---|
| P1 | The steno key path takes the engine/flash mutex with `K_FOREVER` on ZMK's system work queue (`dictionary_flash_pm.c:53`). The HID console thread holds that mutex for a whole personal-dictionary save, an import commit (budgeted up to 600 s) or a full export. A key pressed meanwhile freezes **all** ZMK work — Bluetooth, split, display — not just steno. | Bounded lock on the key path; on timeout drop the stroke and show "busy" on the display. Release the lock between export chunks and around save validation. |
| P2 | First stroke after the flash idles off polls for up to 1 s on the key thread (`dictionary_flash_pm.c:29-42`); a slower wake silently drops the stroke while status still says READY. | Worker signals a semaphore; report LOADING while waking; rely on the layer hold to pre-wake. |
| P3 | Engine init is lazy and runs on whichever thread needs it first — normally the first steno key. It includes a full validation of the personal dictionary over unmapped QSPI reads and orthography compilation. | Run init from a work item when the flash becomes ready. Also fixes init running on the 4 KB HID stack instead of the 8 KB one. |

### Flash checks and installs

| ID | Problem | Change |
|---|---|---|
| P4 | Cold boot without trust CRCs every image twice (raw, then mapped) — ~4.4 MiB twice at 2 MHz. After an HID install of one image: per-chunk readback, then full raw CRC, then mapped CRC of *all* images. Quad mode is enabled twice per bring-up. | One CRC pass per image at boot; after install, re-check only the image written. |
| P5 | `ZMK_JAVELIN_STENO_QSPI_SCK_DIVIDER` defaults to 16 (2 MHz, proven on the hand-wired build) and no config overrides it, including the routed Boardsource PCB. `tools/qspi` hard-codes divider 16, so the Kconfig advice to "confirm each new value with tools/qspi" cannot be followed. | Add a divider option to the probe; measure on the PCB; set a faster divider in the Boardsource conf. **Needs hardware.** Biggest single win for boot-check and install time. |
| P6 | Installer sends each image twice (preflight pass, then write pass) as hex over 63-byte reports with an ack per 512-byte chunk — roughly 4× the image size on the wire. No per-chunk retry; a failure restarts the image. | **Protocol change.** Device does its own blank/compatibility check; payload as base64 or binary; bounded per-chunk retry; resume from the last good offset. |
| P7 | Personal import downloads the whole dictionary as JSON, merges on the host and uploads a whole new snapshot in 256-byte chunks (half the base-install chunk size). | Raise to 512. Longer term, a device-side merge (**protocol change**). |
| P8 | Every single-entry edit rewrites and revalidates the whole snapshot: two full walks to find two indexes a binary search would give, then three more walks, erase, program, CRC, validate — all under the lock that P1 is about. | Binary search in `EditSource::Prepare`; skip re-validating data that was just write-verified. An append-only edit journal is the real fix but is a format change — see X1. |
| P9 | Flash worker wakes every 7.5 s whenever mapped, even while the steno layer holds the flash on. The diagnostic build polls the UART every 10 ms forever — and battery logging only exists in that build, so battery measurements include a 100 Hz wake-up that release firmware does not have. | Sleep to the exact idle deadline (forever while held). UART RX interrupt → semaphore. |
| P10 | Every wake pays a fixed 50 ms rail settle plus a volatile quad-enable write. | Make the settle time a Kconfig value from measurement; consider setting quad-enable non-volatile at provisioning. **Needs hardware.** |

### Split link and display

| ID | Problem | Change |
|---|---|---|
| P11 | Practice frames carry 5 payload bytes per 20-byte write and every frame is sent twice (150 ms repeat). A word prompt is ~5 writes ×2; Speed drill republishes every second; text is resent when only a counter changed. | **Protocol change.** Drop the blanket repeat (connect burst and heartbeat already re-sync); split state from text; send text only when it changes. |
| P12 | 60 s heartbeat resends full status (or a full Practice frame plus its repeat) forever, forcing a full redraw on the right. | Send a short version/hash; resend in full only on mismatch. |
| P13 | Right half sets font/style on labels before checking whether the text changed, on every key press, battery event and heartbeat. In full-render mode each invalidation is a whole-panel render, rotate and SPI write. *(LVGL invalidation behaviour: verify.)* | Cache last text + font per label and return early; skip `screen_update` when inputs are unchanged. |
| P14 | Results-card timer keeps flipping (full-screen flush every 2.5 s) while the keyboard is idle. | Pause it when activity is not ACTIVE. |
| P15 | Spinlocks (interrupts off) held across Practice frame validation and struct copies on the right half; mode / layer name / practice state are three separate atomics that can tear for a frame. | Both contexts are threads: use a mutex and one struct. |
| P16 | Tape sequence number is one bit, so two lost strokes can attach text to the wrong stroke. Truncation at 12 characters is silent. WPM stays on screen indefinitely after the last stroke. | 2–3 bit sequence (spare bits exist), a truncation flag, a WPM timeout. **Protocol change** (small). |

### Practice storage

| ID | Problem | Change |
|---|---|---|
| P17 | Each progress save rewrites one 3,624-byte settings value — nearly a whole 4 KB NVS sector — though at most 10 words changed. Entering and leaving a drill without answering still costs a write. The record is copied onto the stack on save and load, which is why both stacks are raised to 8 KB on the RAM-tight left half. The format is at its size ceiling (~315 words). | Split into a small header key plus per-block word keys; save only dirty blocks; don't mark dirty until the first answer; static scratch buffer under the mutex. Owner approved a fresh progress record instead of migration code. Then re-measure and lower the stack sizes. |

### Smaller

| ID | Change |
|---|---|
| P18 | `HidWriter` buffers up to 63 bytes and flushes at end of command instead of one padded report per `Write()` (replies are currently 2–3 reports each). |
| P19 | `copy_xip_bytes` does one word load + barrier per byte; copy word-wise. This is on the per-stroke personal-dictionary lookup path. |
| P20 | Each key press takes the flash mutex twice (init check, then process); fold into one. `apply_layer()` re-runs and re-sends on every layer event including momentary layers; cache the last state. |
| P21 | `behavior_javelin_steno.c` and `behavior_tap_first.c` compile on the peripheral half with stubs; compile the engine-facing part central-only. Add the missing NULL check in `behavior_tap_first.c:10-20`. |

### Integrity of trusted images

| ID | Problem | Change |
|---|---|---|
| P22 | Waking the flash uses the quick check (chip ID, magic, a few lookups), which is right for wake latency, but once an image is trusted nothing ever re-reads it, so slow corruption would go unnoticed. | Background scrub: CRC the base images in small slices at low priority while the flash is already powered, about once a day of use. On mismatch clear trust (via the `lf_trust_*` helpers from S3) and fire the ready hook. Never run it on the key thread or hold the flash awake for it. |
| P23 | The deep-sleep trust marker folds the image fingerprint to 8 bits, so a firmware change across deep sleep is mistaken for the same images 1 time in 255. | Compare the full fingerprint from the stored settings record after wake when the 8-bit marker matches. |

## 7. Phase 4 — CI, tests and tooling

| ID | Problem | Change |
|---|---|---|
| T1 | Dictionary download + compile is copy-pasted in two jobs and runs 7 times per push (6 downloads each). The Lapwing base — whose CRC is baked into release firmware — is the only image fetched **without** a sha256 check and never run through `verify_mapped_dict.py`. | `tools/dictionaries/prepare_lapwing.py` (fetch, hash, compile, verify). One `dictionaries` job publishes the three images as an artifact for the others. |
| T2 | `west update` (ZMK + Zephyr + modules) runs uncached in all 12 matrix jobs; the Boardsource repo is fully cloned in 7. | `actions/cache` keyed on `config/west.yml`; fetch the pinned Boardsource commit by SHA at depth 1. |
| T3 | Actions pinned by tag (`@v4`) in a workflow with a `contents: write` release job; container `zmk-build-arm:stable` floats; `git clone` then checkout. | Pin actions by commit SHA and the container by digest. |
| T4 | Triggers: no `pull_request`; no concurrency group, so superseded pushes keep running; a tag re-runs all 15 jobs for an already-built commit. | Add `pull_request` and cancel-in-progress for non-tag refs. |
| T5 | Display preview test runs only inside one matrix job and exits 0 with a warning if a tool is missing — it can be silently disabled by an image update. | Move to `host-tests` with LVGL fetched at the pinned revision; fail hard. |
| T6 | No test runner: the list of tests exists only in YAML, across three directories, some needing positional arguments. A new test file is not run unless the YAML is edited. | `tools/tests/run_all.py` (or pytest) that CI calls once. |
| T7 | Eight tests slice functions out of C sources by brace-counting or `"\n}"` search, then paste them into a harness. A rename or a brace in a string gives a Python `ValueError`. One stub replaces CRC-32 with `crc*31+data`, so host/firmware CRC agreement is never tested. Compiler names vary (`cc`, `c++`, `clang`, `clang++`, `gcc`) with no override. | Move the tested functions into small headers the tests `#include` (the pattern four tests already use). Use a real CRC-32. Do this **before** phase 2 renames, which would otherwise break every slice. |
| T8 | `tools/qspi` scratch/trace/mapped variants are never built in CI. | Build one variant, or delete them (with D2). |
| T9 | No `LICENSE` for the repo's own code; no `pyproject.toml`/requirements (needs Python ≥ 3.10 for `int.bit_count`, plus hidapi / pyserial / Pillow); no `.clang-format`, ruff or `.editorconfig`. | Add them; lint in CI. **You choose the licence.** |
| T10 | Nothing pins the compiled image CRCs, though the installer's "rebuild locally and match the firmware" design depends on the compiler being deterministic. | A test that asserts the three image CRCs. |
| T11 | `render_keymap.py` output is never checked against the committed SVGs. | CI "render is up to date" check (after B13). |
| T12 | Prepare scripts re-download even when a file with the right hash is on disk. | Check the hash first. |

## 8. Phase 5 — optional, larger

Do only if wanted; each is independent.

| ID | Item | Notes |
|---|---|---|
| X1 | Personal-dictionary edit journal | Turns a full-snapshot rewrite per edit into an append. On-flash format change with migration. |
| X2 | 1-bit fonts on the right half | Eight 4bpp fonts (~100 KB, estimated) are thresholded to 1 bit anyway and drawn twice for faux bold. 1bpp ASCII fonts would allow a 1.4 KB draw buffer instead of 10.9 KB and a smaller LVGL pool. Changes how text looks. |
| X3 | Prune the engine source list | ~60 engine files are compiled that this build probably never calls. Trial-link to find out; also test whether `-fpermissive`, `-Wno-invalid-constexpr`, `M_PI` and `JAVELIN_PLATFORM_ZEPHYR` are still needed. Build-time win only (the linker already drops unused code). |
| X4 | Generate the Windows host-layout table | 548 hand-kept lines that are mechanically "strip the accent" (Unicode NFD base letter). Add compile-time sortedness asserts to both tables either way. |
| X5 | Compile-time ties to the engine | `static_assert` that the keymap's steno key IDs match the engine's `StenoKey` enum; designated initialisers in `font_stub.cc`; look up the GATT attribute instead of the literal index 6 in `javelin_ble_hid.c`. |
| X6 | Framed serial commands | In diagnostic builds a single stray byte (`E`, `e`, `K`) on the shared logging port erases a dictionary. Require a framed line for anything that writes. Moot if D1 retires serial provisioning. |
| X7 | Renames (D3) | `dictionary_flash*` → generic, `squid_qspi_reactivate` gets a prefix, `diag.[ch]` → flash-specific name. Pure rename commit, last. |
| X8 | Generate `pet_effects.h` | ~1,270 hand-maintained lines of hex with no generator, unlike `pet_sprites.h`. Cropping frames to their bounding boxes would save ~3.4 KB. |

## 9. Verification

Per phase:

1. **CI green**: all host-test jobs and all 12 firmware builds.
2. **Size check**: compare the "flash and RAM" and "memory by owner" summaries CI already prints, before and after. Phases 0–2 should be flat or smaller; any growth is explained in the PR.
3. **Hardware checklist** (both board families where the phase touches board-specific code):
   - Cold boot → steno layer → first stroke translates.
   - Leave idle past 30 s on the Qwerty layer → return to steno → first stroke translates.
   - Deep sleep and wake from each half.
   - `stenosquid.py --status`, `--install` on a blank flash, `--repair`, `--import`.
   - Browser tool over USB and over Bluetooth: toggle a dictionary, add and remove an entry, export.
   - Type steno **while** an export or import is running (phase 3, P1).
   - Practice: one run of each drill; results screen; progress survives a reboot.
   - Right display: layer names, stroke tape, hint, pet animations, Practice screens.
   - Diagnostic build: `S`, `D`, `Z`; battery log export.
4. **Protocol-change phases**: also test old host tool against new firmware (and the reverse) and confirm the mismatch is reported cleanly.

## 10. Suggested order and size

| Phase | Content | Rough size | Risk |
|---|---|---|---|
| 0 | Bugs B1–B17 + doc fixes | Small; many few-line fixes | Low |
| 4a | T7 (test seams), T1, T6 | Medium | Low — do before phase 2 so renames don't break tests |
| 1 | Cruft C1–C15 | Medium; mostly deletions | Low |
| 2 | Dedup S1–S16 | Large | Medium |
| 3 | P1–P3 first, then P4–P10, P11–P16, P17, P18–P21 | Large; several need hardware | Medium–high |
| 4b | Rest of T | Medium | Low |
| 5 | Optional X items | Per item | Varies |
