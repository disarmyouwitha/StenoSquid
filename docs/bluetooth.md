# Manager over USB and Bluetooth

Open `StenoSquidManager.html` in Chrome or Edge. Plug in the left half over USB
or pair the keyboard in your computer's Bluetooth settings, then click
**Connect**. Manager uses the existing HID pairing, shows the active
connection, and prefers USB when both are available. Select **Reconnect** after
plugging in or unplugging USB to switch transports.

| Operation | USB | Bluetooth |
| --- | --- | --- |
| Scan Memory, dictionary controls, entry edits, exports, full backup | Yes | Yes |
| Compact a dictionary, Compact Memory and Reclaim | Yes | Yes |
| Install or remove factory dictionaries | Yes | Yes. Large transfers show a speed warning. |
| Preview and import dictionary JSON | Yes | Requires USB |
| Preview USB Repack, Repack, and restore the original backup | Yes | Requires USB |

## Measured transfer speeds

Measured through the Manager's HID console on a Boardsource left with a Mac.
USB and Bluetooth returned matching checksums.

| Operation | USB | Bluetooth |
| --- | ---: | ---: |
| Read a 1,463-byte catalog, median | 26 ms | 361 ms |
| Read 64 KiB from flash | 41.0 KiB/s | 3.08 KiB/s |
| Upload 16 KiB to a staged extent, excluding erase | 39.5 KiB/s | 1.71 KiB/s |

The numbers measure console transfers. A complete install also includes
preparation, erasing, verification, and catalog publication. Separate
RAM-only probes reached roughly 15–16 KiB/s through binary GATT and L2CAP on
the test setup. They remained below production USB speeds and did not measure
flash installation or physical typing. Manager ships on the existing HID
transport.

## Faster Bluetooth with a short connection interval (lab option, measured on the nice!nano with Windows)

The slow Bluetooth uploads were a link-parameter problem, not a protocol one. The keyboard asks hosts for a 15 ms interval and a
peripheral latency of 30 (low power for typing); the vendor console's host writes wait for the keyboard's answer, so one console
round trip took about 120 ms and a 260 KB upload took 417 s (0.61 KiB/s). Asking for 7.5 ms and latency 0 while the Manager is
talking:

| Link (nice!nano, Windows, JS client through `hidapi`) | Console round trip | Lapwing Proper Nouns (260 KB) |
| --- | --- | --- |
| 15 ms interval, latency 30 (today) | 60-120 ms | 417 s (0.61 KiB/s, measured earlier in Chrome) |
| 7.5 ms interval, latency 0 | 30 ms | 45 s (5.8 KiB/s), window 8 or 16, no NAKs, rewinds or stalls |

The rate is now one console write per connection event (Windows sends one HID write per event), so a bigger window does not help
and 7.5 ms is the shortest interval a Bluetooth link allows. About 6 KiB/s is the ceiling of HID over GATT on this host; a
Lapwing Base upload (1.8 MB) would take about 5 minutes instead of an hour. More needs a custom GATT service (Web Bluetooth); an
earlier RAM-only probe reached 15-16 KiB/s that way.

`CONFIG_ZMK_STENO_SQUID_BLE_FAST_LINK` (off by default; on in `config/boardsource-wireless-corne/lab-ble.conf`) requests the fast
link on the first console write and ZMK's normal parameters again after ten seconds without one. Typing does not trigger it. Not
yet checked: battery use, macOS (it may grant a longer interval), the Boardsource, a Chrome upload. `CONFIG_ZMK_STENO_SQUID_LAB_BLE_TUNING` adds
`native_install lab-ble info | params MIN MAX LAT TO | phy2m | datalen` for experiments (`phy2m` and `datalen` returned -5 on Windows). A
first read on a fresh Bluetooth connection fails about 1 time in 12 (a short or malformed reply); it happens with and without
the fast link and was already present.
