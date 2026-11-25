# Boardsource hardware bringup — 2026-11-17–18

The Boardsource Wireless Corne SMT pair passed the functional tests below with
an Adafruit #5634 W25Q128 breakout, 100 nF supply decoupling, and a 10 kΩ CS
pull-up. The right half has a nice!view. These wired-prototype tests used **2 MHz**.

## Verified results

| Check | Evidence |
|---|---|
| Left USB recovery | BS Keyboard / NRF52840_KB bootloader identified; repeated application/probe flashing succeeded. |
| Left rail polarity | Empty header on USB: P0.31 LOW gives 3.32 V; HIGH gives approximately 0 V. **Active-low**. |
| Battery supply | Owner measured approximately 3.3 V on battery, USB unplugged; physical switch off gives approximately 0 V. Battery itself measured approximately 3.97 V. |
| Loaded USB supply | Approximately 3.3 V across contacts 5/6 with the flash breakout connected. This is a DC reading, not transient qualification. |
| Flash identification | JEDEC `EF 40 18`. |
| Single-line read | FASTREAD at address zero passed; the first sixteen bytes were FF. |
| Quad-read comparison | A blank 64 KiB comparison was inconclusive. The later patterned test passed with lane coverage `0xf`, exercising both states on IO0–IO3. |
| Program and erase | Verified the last 4 KiB sector at `0xFFF000` was blank; programmed/read back 256 varying bytes; erased and verified all 4,096 bytes FF again. |
| Dictionary installation | Lapwing, Plover and extras installed over USB HID; a separate full readback CRC verified all three. |
| Runtime | Installer reports `2 READY`; `KAT` resolves to `cat` from Lapwing Base. Personal store ready, zero entries and zero integrity errors at inspection. |
| Split/display | Owner confirmed Pop's install screen on nice!view and ordinary typing from both halves. |
| Onboard output and restart | Owner confirmed repeated KAT/cat output and output again after restart/bootloader exit. |
| USB flash idle/wake | Outside Javelin, rail shut down after the expected 30-second idle period. Owner observed approximately 0.1 V by the first manually timed reading (roughly two seconds after the fall), then 0.05 V around eight seconds. Returning to Javelin restored approximately 3.3 V and the first KAT stroke worked. |
| Battery/Bluetooth idle/wake | Owner confirmed the same apparent rail behavior and immediate Javelin typing after idle. Exact battery voltage during this test and an explicit repeat count were not supplied. |

The right display works with active-low external power. Its polarity was inferred
from the shared vendor design; a separate right-header voltage/polarity measurement
was not performed.

## Matching installed dictionaries

| Image | Bytes | CRC32 |
|---|---:|---|
| Lapwing | 1,830,444 | `00f2d636` |
| Plover | 2,339,620 | `3e8f8d88` |
| Extras | 469,812 | `98af1ecc` |

The tested everyday pair was built with the bringup fixes and the existing
production storage layout. The hybrid catalog/overlay/journal work is a separate
host-only prototype and was not installed on the keyboard.

## Firmware corrections found on hardware

1. **Accessory power is active-low.** Change the left QSPI and right external-power
   overlays to `GPIO_ACTIVE_LOW`; the original active-high assumption left the
   flash rail at zero volts.
2. **Provisioning must wake an idle blank chip.** The first install failed before
   writing dictionary data because the flash had powered down. Every provisioning
   command now attempts acquire/wake, including commands for an unmapped blank
   chip. Upload chunks count as activity. The mapped-acquire/fallback-lock
   contract is preserved. A regression reproduced the failure before the fix and
   passes after it; existing transfer, bounds, CRC and repair checks also pass.
3. **Rail diagnostics must support both polarities.** The rail test now identifies
   either physical enable level, compares it with devicetree, and times rise/fall
   using that level instead of treating active-low hardware as an error. Host tests
   cover both polarities, mismatches, absent supply and a stuck-high sense pin.

An initial diagnostic serial status request produced logs but no command reply.
That path remains unverified. Successful installation and runtime verification
used USB HID; the installed everyday firmware has Studio USB rather than the
logging diagnostic interface.

## Limits and next checks

- Multimeter readings establish low settled off voltage, not the exact time to
  cross 0.8 V or its required dwell. The firmware's 500 ms retry-off interval is
  not qualified by the manually timed readings. No scope trace was captured.
- Full keyboard deep sleep, cold battery cycling, and a saved personal-entry
  persistence test were not completed. Flash idle-off is distinct from whole
  keyboard sleep. See the [power test procedure](boardsource-power-validation.md).
- Precise current/transient margin, battery calibration, exhaustive full-array
  testing, higher clock speeds, and the 32/64 MB parts remain unqualified.
- Final populated daughterboard fit and its effect on both Bluetooth links need
  the actual PCB. Template, connector and assembly checks remain separate.

C2 and R2 remain absent in the intended initial circuit. No additional capacitor
or discharge resistor was justified solely by the multimeter observations.

## Local evidence and rollback

Ignored local artifacts under `build/boardsource-bringup-169d1c9/` include the
standard/quad/scratch probe serial logs, dictionary install and verification logs,
bootloader information, and `installed-manifest.json` with board identities and
firmware hashes. Everyday rollback UF2s are in
`build/boardsource-corrected-left/zephyr/zmk.uf2` and
`build/boardsource-corrected-right/zephyr/zmk.uf2`. These paths describe local
session evidence; they are not downloadable release artifacts.

[Breakout wiring illustration](images/boardsource-wiring/boardsource-adafruit-breadboard.png)
uses component-side orientation and net labels; its breadboard holes are
illustrative, not a hole-by-hole placement prescription.

## 32 MHz qualification on the 16 MiB PCB

On 2026-11-23, the assembled W25Q128JV daughterboard passed single-line and
quad-read comparison at 2, 8, 16, and 32 MHz. Each 256-byte sample exercised
both logic states on all four IO lanes. At 32 MHz the everyday left firmware
passed USB and Bluetooth typing, a battery cold boot, and Bluetooth wake after
16 minutes idle. Full Native verification passed after each boot and wake
with the 1,830,444-byte Lapwing Base installed.

The production driver programmed a 1 KiB pattern in a blank unowned 64 KiB
block and verified its bytes. It erased the block, verified all 64 KiB blank,
and passed another full Native integrity check. Catalog generation and contents
were unchanged. Page-program, readback, and erase commands use the same clock
divider. The flash chip's internal program and erase times are independent of
that divider.

A temporary 32 MHz diagnostic build replayed 132 strokes at 299.95 WPM and
a 44-stroke burst at 349.95 WPM. Both USB captures matched the expected text
and stroke order with zero input overflows and zero deadline overruns.
Lapwing Base and the personal dictionary were active, with hints enabled.
The everyday firmware was restored and verified afterward. These replays
inject chords on the left, so physical key scanning, split and BLE delivery at
these speeds, and the 16-dictionary worst case remain separate tests.

## 64 MiB PCB address check at 32 MHz

The same Boardsource left firmware detects the W25Q128JV (16 MiB) and
W25Q512JV (64 MiB) by JEDEC ID. The larger flash uses four-byte addresses.
On the 64 MiB PCB, USB provisioning installed Lapwing Base, verified the
full pool, and reopened the catalog after reboot. Eight unique write/readback
patterns across the four 16 MiB address regions matched, including the last
usable block at `0x03fe0000`. The last physical block refused a staged erase.
The scratch blocks were erased again, Native verification passed, and
`KAT` resolved to `cat`. The map reported 992 free 64 KiB blocks with three
held in reserve.

The dual-capacity firmware also reopened the existing catalog on the 16 MiB
PCB without reprovisioning. Native integrity verification passed, the map
reported 224 free blocks, and `KAT` resolved to `cat`. Battery cold boot and
sleep/wake on the 64 MiB PCB still need qualification.
