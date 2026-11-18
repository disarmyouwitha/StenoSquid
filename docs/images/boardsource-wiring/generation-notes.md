# Boardsource / Adafruit breadboard wiring illustration

Generated with the built-in ImageGen tool. Final file: `boardsource-adafruit-breadboard.png`.

Use the final image only. Earlier generated drafts had incorrect wire endpoints and were rejected. The final is a net-labeled breadboard-style illustration, not a numbered-hole placement guide. C1 is drawn symbolically to make its two terminals unambiguous. Keep the actual decoupling capacitor close to the breakout supply pins and use short wires.

## Verified sources

- [Adafruit #5634 product](https://www.adafruit.com/product/5634)
- [Adafruit pinouts](https://learn.adafruit.com/adafruit-qspi-dip-breakout-boards/pinouts)
- [Adafruit native PCB](https://github.com/adafruit/Adafruit-QSPI-DIP-Breakout-PCB)
- Project host map: `config/boardsource-wireless-corne/README.md`.

The native board has no onboard passives. Front-view pad layout with white CS square at upper left: left column CS, IO1, IO2, G; right column 3V, IO3, CLK, IO0. The labeled back is mirrored relative to this view. Add 10 kΩ CS-to-switched-3V and 100 nF switched-3V-to-GND. IO2 and IO3 connect separately to their assigned GPIOs; leave optional C2 and R2 out.

## Original generation prompt

Use case: scientific-educational.
Create a clear high-resolution landscape breadboard wiring illustration for an electronics beginner, titled "Steno Squid — Boardsource to Adafruit QSPI".
Reference images: first photo is the FRONT/component-side of Adafruit product #5634 W25Q128 16 MB QSPI DIP breakout; second photo is its BACK and is mirrored relative to the front. Use these only as hardware references. The diagram MUST use TOP / COMPONENT VIEW with the white CS square at TOP LEFT. Do not copy backside orientation.
Main composition: a white solderless breadboard with a center trench, illustrated cleanly in the Fritzing instructional style, flat orthographic top-down view, crisp labels, white background, generous space. Black small Adafruit breakout centered straddling the trench. It has exactly TWO columns of FOUR pins. FRONT-view pad positions MUST be:
LEFT column from top to bottom: CS, IO1, IO2, G.
RIGHT column from top to bottom: 3V, IO3, CLK, IO0.
These are net labels placed beside each pad, not generic pin numbers. White square around CS top-left. W25Q128 chip centered. Label "Adafruit #5634 • W25Q128 • 16 MB" underneath.
Show breadboard hole rows 14,15,16,17 and column letters A–E to left of trench and F–J to right. Breakout pins occupy E14,E15,E16,E17 and F14,F15,F16,F17 respectively. The breadboard's A–E group in each numbered row is connected, and F–J group in each numbered row is connected; the trench separates those groups. NO power-rail usage is necessary; do not use long red/blue supply rails.
On left side of composition show a large abstract connector legend "Boardsource LEFT expansion header" as eight numbered colored terminals, explicitly label "Pin numbers — verify pin 1 on your board". This is a logical breakout of the connector, NOT a depiction of the physical keyboard/header orientation.
Route exactly eight individually distinguishable colored wires to the breadboard, with clean bends and no ambiguous junctions. Only a dot indicates a joined net; crossings without dots are unconnected. Correct netlist, absolutely invariant:
1 / P0.07 -> CS (left top, row14)
2 / P0.21 -> IO0 (right bottom, row17)
3 / P0.12 -> IO3 (right second, row15)
4 / P0.23 -> IO1 (left second, row15)
5 / switched VCC -> 3V (right top,row14), RED wire
6 / GND -> G (left bottom,row17), BLACK wire
7 / P0.19 -> CLK (right third,row16)
8 / P0.05 -> IO2 (left third,row16).
Route signals to available holes on their matching row and side, NOT into a different row! Keep endpoints visible. Wire labels near endpoints should read header number + net. Use red for power, black for ground, yellow CS, green IO0, blue IO3, purple IO1, orange CLK, teal IO2. Use no signal wires tied directly to power.
Show exactly two discrete passives clearly, physically mounted in spare breadboard holes above the breakout with short jumpers to the appropriate nets. R1 is an axial 10 kΩ resistor ACROSS CS and switched 3V (parallel pull-up, NEVER in series with the CS wire). Place R1 across D10 and G10; jumper D10 to D14 = CS and jumper G10 to H14 = 3V. Clearly label "R1 10 kΩ — CS to 3V".
C1 is a small 100 nF nonpolar ceramic capacitor ACROSS switched 3V and GND. Place its two leads in I11 and I12 (different rows). Connect J11 to J14 = 3V and J12 to A17 = GND by short routed jumper. Label "C1 100 nF (0.1 µF) — 3V to GND". Ensure C1 is NOT connected to IO0 or another data row. Small inset schematic beside the board reinforces R1 between 3V and CS and C1 between 3V and GND, no other components.
Include a legible compact eight-row authoritative table on the RIGHT with columns "Host pin", "GPIO / rail", "Breakout":
1 P0.07 CS
2 P0.21 IO0
3 P0.12 IO3
4 P0.23 IO1
5 switched VCC 3V
6 GND G
7 P0.19 CLK
8 P0.05 IO2.
Bottom footer exact readable notes: "Disconnect USB and battery before wiring." "3.3 V only • Keep wires short • Leave C2 and R2 out." "IO2 and IO3 each go to a GPIO — do not tie them to 3V." "White square = CS; labels are shown in component-side orientation."
No LEDs, no breadboard power supply, no Arduino, no extra resistors, no electrolytic capacitor, no speculative physical keyboard outline. Prioritize correct topology, visible endpoints, accurate board orientation and readability over photographic realism.

## Refinements used for the final image

1. Replace long crossing host cables with eight individually labeled wire stubs; preserve the authoritative mapping table. Remove misleading hole coordinates and mark the breadboard holes illustrative. Show explicit passive endpoints.
2. Correct Host 5/VCC so its red wire lands only on 3V, never on the blue IO3 net.
3. Replace the ambiguous ceramic disk rendering with a vertical two-plate capacitor symbol: upper terminal to 3V, lower terminal to GND, white insulating gap between plates. Preserve all remaining connections.
