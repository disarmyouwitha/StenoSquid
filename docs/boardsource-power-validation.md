# Boardsource flash power validation

The installed everyday left firmware uses active-low P0.31, a 2 MHz QSPI clock, 50 ms startup settling, and flash idle-off after 30 seconds outside Javelin. The whole keyboard's battery deep-sleep timeout is 15 minutes. Staying in Javelin holds flash power on during ordinary idle; whole-keyboard deep sleep can still shut it down.

## Evidence before this test

- Approximately 3.3 V unloaded on USB and battery; physical switch-off gives approximately 0 V.
- Approximately 3.3 V on USB with the flash breakout attached.
- JEDEC, patterned single-line/quad reads on all four lanes, scratch program/erase, three installed dictionary CRCs, split display, both-half typing and onboard KAT output after restart passed.
- The larger-capacity parts, final daughterboard layout, transient load margin and timing are not qualified by these tests.

## First test: firmware idle-off on USB

Keep USB connected and the physical power switch ON. Close dictionary/editor clients; do not send HID lookup/status commands during the idle measurement, since they can wake or hold the flash. Keep C2 and R2 absent initially.

1. Meter: DC volts, leads in COM and V/ohms. Black to host header 6 (GND), red to 5 (switched VCC). Use stable connections and do not move wiring while powered.
2. Select Javelin with the right middle thumb key. Record the powered voltage.
3. Return to Qwerty using the left outer thumb key. Begin observing immediately. Do not change the physical power switch or disconnect USB.
4. Around 30 seconds after leaving Javelin, note when voltage starts to fall and how it changes. Record stable off voltage at about 35 and 45 seconds after leaving the layer. Multimeter display refresh limits timing accuracy.
5. Select Javelin again. Confirm supply recovery and the first KAT stroke's output. Repeat several times, keeping the keyboard powered throughout.

This distinguishes removal of power from the accessory from shutting down the whole keyboard. The flash contents should persist because the array is nonvolatile. Failure to reach a low off voltage suggests residual charge or backpower and requires investigation before treating the power cycle as a reset.

## Timing qualification

For the reviewed 16 MB Winbond part, full power-reset qualification requires VCC below 0.8 V for at least 100 microseconds. A multimeter's eventual low reading does not establish this dwell or fast rise/fall timings. The firmware retry/bench power-off interval is 500 ms; a scope trace can determine whether rail decay leaves enough time below the threshold. The digital diagnostic sense threshold is also not an analog 0.8 V measurement.

With a scope, use a suitable DC-coupled probe on header 5, ground on 6, falling-edge capture, and preserve the waveform so time to below 0.8 V and dwell before power restoration can be measured. Record probe loading and actual passive population. Do not add C2 to cure discharge; extra capacitance generally slows it. R2 is a reserved discharge option to evaluate from measured behavior.

## Follow-up tests

- Repeat idle-off/wake with USB unplugged, both halves powered by batteries, and host Bluetooth working; measure loaded battery rail voltage.
- Complete multiple true cold power cycles, then verify base CRCs again over USB.
- Save a temporary personal translation, power-cycle, confirm persistence and remove the temporary entry.
- Leave both halves untouched on battery for more than 15 minutes; check flash off-state and wake/reconnection using a key on each half, then KAT output.
- Check host and split Bluetooth reliability in normal positions while typing and accessing flash. Final PCB copper/antenna effects require the populated daughterboard.

## Measurements

2026-11-18, installed everyday firmware, USB connected, multimeter across host contacts 5/6:

- Owner confirmed approximately 3.3 V with Javelin selected; multimeter only, no scope reported.
- During the instructed Qwerty idle-off measurement, owner reported: "within 2 seconds it fell to .1 and then slowly fell to .05 after 8 seconds".
- Owner clarified that the rapid drop occurred after the expected 30-second idle timeout; the timer was started manually as quickly as possible when the fall occurred. Approximately 0.1 V by the first roughly two-second reading, approximately 0.05 V around eight seconds. These are human-timed observations, not measured crossing times. Both voltages are below the reviewed 0.8 V flash-reset threshold.
- These multimeter observations establish a low settled off voltage; they do not qualify discharge within the firmware's 500 ms retry interval, fast transient behavior or a scope-measured reset dwell.
- Owner confirmed Javelin re-entry restored approximately 3.3 V and the first KAT stroke produced cat. First USB idle-off/discharge/wake cycle passes functionally. Repeated battery cycles and deep sleep remain untested.

### Battery/Bluetooth idle-off and wake — 2026-11-18

The owner reports that, with USB removed and the keyboard operating over Bluetooth, the flash rail powers up and down at the same apparent rate as on USB and Javelin accepts typing immediately after flash idle. This confirms functional battery/Bluetooth accessory idle-off and wake. Exact loaded battery voltage, an explicit cycle count and subsecond decay timing were not provided, so no numeric values or repeat count are inferred. Full keyboard deep sleep is the next pending test.
