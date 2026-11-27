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
| Merge compact dictionaries | Yes | Requires USB |
| Preview USB Repack, Repack, and restore the original backup | Yes | Requires USB |
| Initial provisioning and Reprovision | Yes | Requires USB |

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

Shorter connection intervals improved Windows upload throughput. The keyboard asks hosts for a 15 ms interval and a
peripheral latency of 30 (low power for typing); the vendor console's host writes wait for the keyboard's answer, so one console
round trip took about 120 ms and a 260 KB upload took 417 s (0.61 KiB/s). Asking for 7.5 ms and latency 0 while the Manager is
talking:

| Link (nice!nano, Windows) | Console round trip | Lapwing Proper Nouns (260 KB) |
| --- | --- | --- |
| 15 ms interval, latency 30 (today) | 60-120 ms | 417 s (0.61 KiB/s, measured earlier in Chrome) |
| 7.5 ms interval, latency 0, JS client through `hidapi` | 30 ms | 45 s (5.8 KiB/s), window 8 or 16, no NAKs, rewinds or stalls |

Windows delivered about one HID write per connection event at the fast
settings. Increasing the transfer window from 8 to 16 gave the same rate.
On the 64 MiB Boardsource with macOS, the fast-link lab build negotiated
11.25 ms with zero peripheral latency. Median console round-trip time was
22.6 ms with fast link versus 22.5 ms on the regular lab build (24 samples
each). Binary session setup and cancellation succeeded on both USB and
Bluetooth. The Mac runs sent no bulk data.

`CONFIG_ZMK_STENO_SQUID_BLE_FAST_LINK` defaults off. The lab fragment
`config/boardsource-wireless-corne/lab-ble.conf` enables it. The firmware
requests the fast parameters on a Manager console write and requests the
normal parameters after ten seconds of inactivity. Typing alone leaves the
connection settings alone. The Mac still reported fast parameters after
12 seconds of inactivity. Idle return, battery use and fast uploads through
Chrome still need measurement. A short or malformed first reply on a fresh
Windows Bluetooth connection was observed with both settings.

## Bluetooth qualification gate

The target is at least **50 KiB/s of useful upload payload** on a
checksum-verified staged dictionary upload through the real Chrome path.
Report stream throughput and total elapsed time, including erase,
verification and publication. The **62.5 KiB/s** JS/Chrome benchmark was
over USB. Treat console round-trip, RAM-only, staged flash and full
Manager results as separate measurements.

1. Reuse the existing RAM-only GATT probe with the current bonded host and
   both halves running. On Windows and macOS, record the negotiated MTU,
   PHY and interval, the useful bytes and writes delivered per connection
   event, and host send rate versus firmware receive rate. Try larger
   writes without response and a bounded window. Preserve the existing
   pairing, typing and split behavior. Check whether browser permission
   can fit the existing **Connect** flow.
2. If RAM throughput clears 50 KiB/s with headroom, use the same payload
   over USB and Bluetooth in Chrome. Verify data integrity and catalog
   state, and exercise cancellation, reconnect and input during transfer.
   If the RAM probe stalls near the previously measured 15–16 KiB/s,
   record the limiting layer before investing in Manager integration.
3. Once Bluetooth meets the upload and correctness gate, enable the
   remaining Manager operations over Bluetooth. **USB Repack** and
   **Reprovision** remain USB-only, including recovery for those flows.
