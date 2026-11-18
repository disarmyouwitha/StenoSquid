# Boardsource hardware bringup — 2026-11-17–18

The Boardsource Wireless Corne SMT pair passed the functional tests below with
an Adafruit #5634 W25Q128 breakout, 100 nF supply decoupling, and a 10 kΩ CS
pull-up. The right half has a nice!view. QSPI remains at **2 MHz**. These results
cover the wired 16 MB prototype, not the final daughterboard or larger chips.

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
