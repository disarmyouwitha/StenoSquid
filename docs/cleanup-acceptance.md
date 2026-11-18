# Cleanup acceptance

The cleanup changes both split protocols and the USB installer protocol. Update
both halves and use the matching `stenosquid.py`. Protocol versions reject the
old installer before writing. Existing dictionaries, pairing settings and
checkbox selections are retained. Practice XP and word progress start fresh,
as approved; there is no migration reader.

## Automated checks

The native suite exercises bounded flash access, interrupted-chord cancellation,
streaming exports, installer retry/resume, CRC agreement, background scrub,
Practice scoring and block saves, split fragmentation/loss/reorder, tape sequence
and truncation, HID/BLE ownership, personal-store power-loss recovery and real
LVGL rendering. Image sizes and CRCs remain fixed. Generated keymaps are checked
against the committed SVGs.

The left-default build uses 449,480 bytes of internal flash and 207,798 bytes of
RAM (79.27%). Diagnostic uses 515,304 bytes of flash and 220,222 bytes of RAM (84.01%).
Right-default uses 444,524 bytes of flash and 89,232 bytes of RAM
(34.04%). Compared with phase 2, left RAM drops 3,584 bytes despite adding the
engine work queue. External flash layout and dictionary contents are unchanged.
These are linker allocations, not measured runtime peaks.

GCC stack-usage output no longer contains the 3.6 KiB Practice record copy. The
largest first-party frame is 464 bytes (personal export), and Practice block
scratch is static. Individual frame sizes do not prove a whole call-chain peak;
confirm normal operation and diagnostics on hardware before merging.

## Hardware results — 2026-11-17

Both nice!nano halves were flashed with the exact GitHub Actions default UF2s
from commit `996c2aaf46f1c2c2f65fc667802fe88e8c043d4d`.
[All 12 firmware/probe builds and native tests passed](https://github.com/disarmyouwitha/StenoSquid/actions/runs/44113869749).

- The user confirmed ordinary typing, onboard Javelin and all Practice drills.
- After resetting the left half, finishing another round continued the saved
  level/XP rather than starting over. The new block-based saves persist on hardware.
- USB HID returned all three mapped base images, KAT → cat, the existing
  checkbox selections and three intact personal entries with zero integrity errors.

Deep sleep/idle wake, Bluetooth browser editing, large-transfer responsiveness,
blank provisioning/repair and diagnostic hardware checks remain unverified on
this cleanup build. Boardsource electrical/timing checks await the PCB. The
complete acceptance checklist follows; this report does not mark those checks
as passed.

## Full hardware checklist

1. Cold boot, enter Javelin and type KAT. Repeat after at least 30 seconds on Qwerty.
2. Confirm normal typing from both halves, Plover output, display layers, hints,
   stroke text and pet animations.
3. Run each Practice drill, complete a round, then reboot. New XP/stats should
   survive; opening and leaving a menu without answering should not save.
4. Leave both halves idle through deep sleep; wake each and translate again.
5. Over USB and Bluetooth, toggle a dictionary, add/remove a temporary personal
   entry and export. Type during a large export/import: key delivery must remain
   responsive; a chord dropped during a busy write must not become a partial word.
6. Run `stenosquid.py --status`, `--verify`, safe rerun `--install`, and import a
   small personal JSON. Blank install and deliberate base repair also need a
   disposable/test flash or an explicit backed-up test session.
7. In Left-Diagnostic check status/log/counters and battery export. Measure PCB
   clock/settle timing separately before changing production values.

Nice!nano and Boardsource are built in CI. Board-specific electrical behaviour
still needs physical tests on each board; a passing build cannot establish it.
